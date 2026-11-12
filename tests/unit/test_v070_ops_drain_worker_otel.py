"""Offline unit tests for v0.7.0 graceful drain, split worker architecture, and OpenTelemetry."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.downloader.config import Settings, TelemetryConfig, reload_settings
from src.downloader.dependencies import get_job_manager_dependency
from src.downloader.drain import DrainCoordinator
from src.downloader.job_manager import JobInfo, JobManager, JobStatus
from src.downloader.logging_config import CustomJsonFormatter
from src.downloader.main import app
from src.downloader.telemetry import (
    configure_telemetry,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
    shutdown_telemetry,
)
from src.downloader.worker import process_worker_batch_envelope, process_worker_browser_task
from src.downloader.worker_queue import (
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerQueue,
)


class _FakeAsyncRedis:
    """In-memory async Redis stand-in supporting WorkerQueue list/heartbeat operations."""

    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.kv: dict[str, str] = {}

    async def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: int = 0,
        src: str = "RIGHT",
        dest: str = "LEFT",
    ) -> str | None:
        src_bucket = self.lists.get(first_list) or []
        if not src_bucket:
            return None
        item = src_bucket.pop() if src == "RIGHT" else src_bucket.pop(0)
        dst_bucket = self.lists.setdefault(second_list, [])
        if dest == "LEFT":
            dst_bucket.insert(0, item)
        else:
            dst_bucket.append(item)
        return item

    async def lmove(
        self,
        first_list: str,
        second_list: str,
        src: str = "RIGHT",
        dest: str = "LEFT",
    ) -> str | None:
        return await self.blmove(first_list, second_list, timeout=0, src=src, dest=dest)

    async def lrem(self, key: str, count: int, value: str) -> int:
        bucket = self.lists.get(key) or []
        removed = 0
        while value in bucket and (count == 0 or removed < count):
            bucket.remove(value)
            removed += 1
        return removed

    async def blpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                return key, bucket.pop(0)
        return None

    async def brpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                return key, bucket.pop()
        return None

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        self.kv[key] = value
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.kv.pop(key, None) is not None else 0

    async def scan(self, cursor: int = 0, match: str = "*", count: int = 100):
        prefix = match.rstrip("*")
        keys = [k for k in self.kv if k.startswith(prefix)]
        return 0, keys

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.kv.get(k) for k in keys]

    def pipeline(self, transaction: bool = True):
        parent = self

        class _Pipe:
            def __init__(self) -> None:
                self._ops: list[tuple[str, tuple[object, ...]]] = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def rpush(self, key: str, value: str) -> None:
                self._ops.append(("rpush", (key, value)))

            def expire(self, key: str, ttl: int) -> None:
                self._ops.append(("expire", (key, ttl)))

            async def execute(self):
                for op, args in self._ops:
                    if op == "rpush":
                        await parent.rpush(str(args[0]), str(args[1]))
                return [True] * len(self._ops)

        return _Pipe()


class TestGracefulShutdownAndDrain:
    async def test_drain_coordinator_waits_and_times_out(self) -> None:
        coord = DrainCoordinator()
        assert await coord.wait_until_drained(0.1) is True

        async def _slow_req() -> None:
            async with coord.track_request():
                await asyncio.sleep(0.05)

        task = asyncio.create_task(_slow_req())
        await asyncio.sleep(0.01)
        coord.start_drain()
        assert coord.is_draining is True
        assert coord.active_requests == 1

        # Short timeout returns False while request is still active
        assert await coord.wait_until_drained(0.005) is False
        # Waiting long enough completes cleanly
        assert await coord.wait_until_drained(1.0) is True
        await task
        assert coord.active_requests == 0

    def test_drain_middleware_and_health_return_503_while_draining(self) -> None:
        with TestClient(app) as client:
            ok = client.get("/health")
            assert ok.status_code == 200
            assert ok.json()["status"] == "healthy"
            assert ok.json()["draining"] is False

            app.state.drain_coordinator.start_drain()
            try:
                draining_health = client.get("/health")
                assert draining_health.status_code == 503
                assert draining_health.json()["status"] == "draining"
                assert draining_health.json()["draining"] is True

                rejected = client.get("/https://example.com")
                assert rejected.status_code == 503
                assert rejected.json()["detail"]["error_type"] == "service_draining"
                assert rejected.headers["connection"] == "close"
                assert rejected.headers["retry-after"] == "5"
            finally:
                app.state.drain_coordinator.reset()

    async def test_job_manager_wait_for_active_jobs(self) -> None:
        jm = JobManager("redis://localhost:6379")
        assert await jm.wait_for_active_jobs(0.1) is True

        async def _work() -> None:
            await asyncio.sleep(0.04)

        jm._background_tasks["job-1"] = asyncio.create_task(_work())
        assert await jm.wait_for_active_jobs(0.005) is False
        assert await jm.wait_for_active_jobs(1.0) is True


class TestSplitWorkerArchitecture:
    async def test_worker_queue_batch_browser_and_heartbeat_operations(self) -> None:
        redis_stub = _FakeAsyncRedis()
        queue = WorkerQueue(redis_stub)  # type: ignore[arg-type]

        await queue.enqueue_batch_job("job-42", trace_headers={"traceparent": "00-abc-def-01"})
        depths = await queue.get_queue_depths()
        assert depths["batch_pending"] == 1

        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None
        assert env.job_id == "job-42"
        assert env.trace_headers == {"traceparent": "00-abc-def-01"}
        assert (await queue.get_queue_depths())["batch_processing"] == 1

        # Requeue orphaned jobs simulates crash recovery
        requeued = await queue.requeue_processing_jobs()
        assert requeued == 1
        env2 = await queue.pop_batch_job(timeout=1.0)
        assert env2 is not None
        await queue.ack_batch_job(env2.raw_payload)
        assert (await queue.get_queue_depths())["batch_processing"] == 0

        # Heartbeat publish -> list -> remove
        await queue.publish_heartbeat("worker-1", {"active_tasks": 2}, ttl=30)
        workers = await queue.list_active_workers()
        assert len(workers) == 1
        assert workers[0]["worker_id"] == "worker-1"
        await queue.remove_heartbeat("worker-1")
        assert await queue.list_active_workers() == []

        # Remote browser task RPC round-trip
        req = BrowserTaskRequest(kind="screenshot", url="https://example.com", full_page=True)
        await queue.publish_browser_result(
            BrowserTaskResponse(
                task_id=req.task_id,
                success=True,
                content_base64="iVBORw0KGgo=",
                status_code=200,
            )
        )
        resp = await queue.enqueue_browser_task(req, timeout=1.0)
        assert resp.success is True
        assert resp.decode_bytes().startswith(b"\x89PNG")

    def test_api_mode_enqueues_batch_job_instead_of_in_process_execution(self) -> None:
        redis_stub = _FakeAsyncRedis()
        mock_jm = MagicMock()
        mock_jm.redis_client = redis_stub
        mock_jm.create_job = AsyncMock(return_value="job-split-99")
        mock_jm.start_background_job = AsyncMock()
        app.dependency_overrides[get_job_manager_dependency] = lambda: mock_jm

        try:
            with TestClient(app) as client:
                split_settings = Settings()
                split_settings.worker.deployment_mode = "api"
                app.state.settings = split_settings

                resp = client.post(
                    "/batch",
                    json={"urls": [{"url": "https://example.com"}]},
                )
                assert resp.status_code == 200
                assert resp.json()["job_id"] == "job-split-99"
                mock_jm.start_background_job.assert_not_awaited()
                assert len(redis_stub.lists["queue:batch:pending"]) == 1
        finally:
            app.dependency_overrides.clear()

    async def test_worker_processes_batch_envelope_and_browser_task(self) -> None:
        redis_stub = _FakeAsyncRedis()
        queue = WorkerQueue(redis_stub)  # type: ignore[arg-type]
        await queue.enqueue_batch_job("job-w1")
        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None

        mock_jm = MagicMock()
        mock_jm.get_job_info = AsyncMock(
            return_value=JobInfo(
                job_id="job-w1",
                status=JobStatus.PENDING,
                created_at=datetime.now(UTC),
                total_urls=1,
                request_data={"urls": [{"url": "https://example.com"}], "default_format": "text"},
            )
        )
        mock_jm.update_job_status = AsyncMock()
        mock_jm.store_job_results = AsyncMock()

        with (
            patch(
                "src.downloader.worker.process_background_batch_job",
                new=AsyncMock(return_value=([{"url": "https://example.com"}], {"total": 1})),
            ),
            patch("src.downloader.worker.notify_batch_webhook", new=AsyncMock()) as mock_hook,
        ):
            await process_worker_batch_envelope(
                env,
                job_manager=mock_jm,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(4),
                pdf_semaphore=asyncio.Semaphore(2),
            )
        mock_jm.store_job_results.assert_awaited_once()
        mock_hook.assert_awaited_once()
        assert (await queue.get_queue_depths())["batch_processing"] == 0

        # Remote browser task execution
        task = BrowserTaskRequest(kind="pdf", url="https://example.com")
        with patch(
            "src.downloader.worker.generate_pdf_from_url",
            new=AsyncMock(return_value=b"%PDF-1.7 worker"),
        ):
            browser_resp = await process_worker_browser_task(
                task, worker_queue=queue, pdf_semaphore=asyncio.Semaphore(2)
            )
        assert browser_resp.success is True
        assert browser_resp.decode_bytes() == b"%PDF-1.7 worker"

    def test_redis_job_store_parses_redis_and_rediss_uris_without_url_kwarg_error(self) -> None:
        from redis.connection import SSLConnection

        from src.downloader.scheduler.service import _get_redis_job_store

        plain = _get_redis_job_store("redis://redis-host:6380/2")
        assert plain.jobs_key == "scheduler:jobs"
        assert plain.run_times_key == "scheduler:run_times"
        assert plain.redis.connection_pool.connection_kwargs["host"] == "redis-host"
        assert plain.redis.connection_pool.connection_kwargs["port"] == 6380
        assert plain.redis.connection_pool.connection_kwargs["db"] == 2

        tls = _get_redis_job_store("rediss://user:secret@secure-redis:6381/1")
        assert tls.redis.connection_pool.connection_class is SSLConnection
        assert tls.redis.connection_pool.connection_kwargs["host"] == "secure-redis"
        assert tls.redis.connection_pool.connection_kwargs["port"] == 6381
        assert tls.redis.connection_pool.connection_kwargs["db"] == 1


class TestOpenTelemetryIntegration:
    def test_disabled_by_default_returns_none(self) -> None:
        assert configure_telemetry(TelemetryConfig(enabled=False)) is None

    def test_enabled_records_spans_correlates_logs_and_propagates_headers(self) -> None:
        exporter = InMemorySpanExporter()
        cfg = TelemetryConfig(enabled=True, service_name="downloader-test", traces_sample_rate=1.0)
        provider = configure_telemetry(cfg, app=app, extra_exporter=exporter)
        assert provider is not None
        try:
            tracer = get_tracer("test")
            formatter = CustomJsonFormatter()
            with tracer.start_as_current_span("parent.op") as span:
                # JSON log correlation
                record = logging.LogRecord(
                    name="downloader.test",
                    level=logging.INFO,
                    pathname=__file__,
                    lineno=1,
                    msg="hello traced log",
                    args=(),
                    exc_info=None,
                )
                formatted = json.loads(formatter.format(record))
                expected_trace = format(span.get_span_context().trace_id, "032x")
                assert formatted["trace_id"] == expected_trace
                assert "span_id" in formatted

                # W3C header propagation round-trip across Redis boundary
                carrier = inject_trace_headers()
                assert "traceparent" in carrier
                extracted_ctx = extract_trace_context(carrier)
                with tracer.start_as_current_span("child.worker", context=extracted_ctx) as child:
                    child_trace = format(child.get_span_context().trace_id, "032x")
                    assert child_trace == expected_trace

            spans = exporter.get_finished_spans()
            names = {s.name for s in spans}
            assert {"parent.op", "child.worker"}.issubset(names)
        finally:
            shutdown_telemetry()

    def test_lifespan_otel_rebuilds_middleware_and_emits_x_trace_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OTEL_ENABLED", "true")
        reload_settings()
        try:
            with TestClient(app) as client:
                health = client.get("/health")
                assert health.status_code == 200
                assert "x-trace-id" not in health.headers

                blocked = client.get("/http://127.0.0.1:8000/health")
                assert blocked.status_code == 400
                trace_id = blocked.headers.get("x-trace-id")
                assert trace_id is not None and len(trace_id) == 32
        finally:
            monkeypatch.undo()
            reload_settings()

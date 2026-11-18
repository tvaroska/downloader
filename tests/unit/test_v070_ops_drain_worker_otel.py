"""Offline unit tests for v0.7.0 graceful drain, split worker architecture, and OpenTelemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from downloader.config import Settings, TelemetryConfig, reload_settings
from downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
    get_robots_checker,
)
from downloader.drain import DrainCoordinator, install_sigterm_drain_handler
from downloader.job_manager import JobInfo, JobManager, JobStatus
from downloader.logging_config import CustomJsonFormatter
from downloader.main import app
from downloader.ports import BrowserCapacityError
from downloader.telemetry import (
    configure_telemetry,
    extract_trace_context,
    get_tracer,
    inject_trace_headers,
    shutdown_telemetry,
)
from downloader.worker import process_worker_batch_envelope, process_worker_browser_task
from downloader.worker_queue import (
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerQueue,
)
from tests.fixtures.mock_fixtures import FakeRedis


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

    async def test_middleware_counts_in_flight_requests(self) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def _slow_download(*_args, **_kwargs):
            started.set()
            await release.wait()
            return (
                b"<html><body>ok</body></html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 28,
                    "headers": {"content-type": "text/html"},
                },
            )

        mock_http = AsyncMock()
        mock_http.download = _slow_download
        mock_http.close = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        mock_robots.close = AsyncMock()

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            app.state.drain_coordinator.reset()
            app.state.settings = Settings()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                inflight = asyncio.create_task(client.get("/https://example.com"))
                await asyncio.wait_for(started.wait(), timeout=2.0)

                assert app.state.drain_coordinator.active_requests == 1
                assert await app.state.drain_coordinator.wait_until_drained(0.01) is False

                app.state.drain_coordinator.start_drain()
                rejected = await client.get("/https://example.com")
                assert rejected.status_code == 503
                assert rejected.json()["detail"]["error_type"] == "service_draining"

                release.set()
                ok_resp = await inflight
                assert ok_resp.status_code == 200
                assert app.state.drain_coordinator.active_requests == 0
                assert await app.state.drain_coordinator.wait_until_drained(1.0) is True
        finally:
            app.state.drain_coordinator.reset()
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    async def test_lifespan_shutdown_drains_before_closing_clients(self) -> None:
        from downloader.main import _teardown_services

        events: list[str] = []
        coord = DrainCoordinator()
        orig_wait = coord.wait_until_drained

        async def _tracked_wait(timeout: float) -> bool:
            events.append("wait_until_drained")
            return await orig_wait(timeout)

        coord.wait_until_drained = _tracked_wait  # type: ignore[method-assign]

        fake_http = AsyncMock()
        fake_http.close = AsyncMock(side_effect=lambda: events.append("http_client.close"))
        fake_robots = AsyncMock()
        fake_robots.close = AsyncMock(side_effect=lambda: events.append("robots_checker.close"))
        fake_jm = AsyncMock()
        fake_jm.wait_for_active_jobs = AsyncMock(
            side_effect=lambda _t: events.append("wait_for_active_jobs")
        )
        fake_jm.disconnect = AsyncMock(side_effect=lambda: events.append("job_manager.disconnect"))
        fake_metrics = AsyncMock()
        fake_metrics.stop = AsyncMock(side_effect=lambda: events.append("metrics.stop"))

        fake_app = MagicMock()
        fake_app.state.job_manager = fake_jm
        fake_app.state.scheduler = None
        fake_app.state.robots_checker = fake_robots
        fake_app.state.pdf_generator = None

        await _teardown_services(fake_app, Settings(), coord, fake_metrics, fake_http)
        assert events[0] == "wait_until_drained"
        assert events.index("wait_until_drained") < events.index("http_client.close")
        assert events.index("wait_until_drained") < events.index("robots_checker.close")
        assert events.index("wait_until_drained") < events.index("job_manager.disconnect")

    def test_sigterm_handler_triggers_drain_and_chains_previous(self) -> None:
        coord = DrainCoordinator()
        chained: list[int] = []
        orig_handler = signal.getsignal(signal.SIGTERM)

        def _prev(signum: int, _frame: object) -> None:
            chained.append(signum)

        signal.signal(signal.SIGTERM, _prev)
        try:
            restore = install_sigterm_drain_handler(coord)
            try:
                assert coord.is_draining is False
                os.kill(os.getpid(), signal.SIGTERM)
                assert coord.is_draining is True
                assert chained == [signal.SIGTERM]
            finally:
                restore()
            assert signal.getsignal(signal.SIGTERM) is _prev
        finally:
            signal.signal(signal.SIGTERM, orig_handler)

    def test_uvicorn_subprocess_enters_drain_on_sigterm(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "REDIS_URI": "",
            "OTEL_ENABLED": "false",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "downloader.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "info",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10.0
            healthy = False
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while time.monotonic() < deadline:
                    if proc.poll() is not None:
                        break
                    try:
                        resp = client.get(f"http://127.0.0.1:{port}/health")
                        if resp.status_code == 200:
                            healthy = True
                            break
                    except httpx.HTTPError:
                        time.sleep(0.1)
            if not healthy:
                proc.terminate()
                out, _ = proc.communicate(timeout=5.0)
                pytest.fail(
                    f"Uvicorn subprocess did not become healthy (rc={proc.returncode}):\n{out}"
                )

            proc.send_signal(signal.SIGTERM)
            output, _ = proc.communicate(timeout=10.0)
            assert proc.returncode in (0, -signal.SIGTERM), output
            assert "Entering connection drain mode" in output
            assert "All services shut down successfully" in output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5.0)


class TestSplitWorkerArchitecture:
    async def test_worker_queue_batch_browser_and_heartbeat_operations(self) -> None:
        redis_stub = FakeRedis()
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
        redis_stub = FakeRedis()
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
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    async def test_worker_processes_batch_envelope_and_browser_task(self) -> None:
        redis_stub = FakeRedis()
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
                "downloader.worker.process_background_batch_job",
                new=AsyncMock(return_value=([{"url": "https://example.com"}], {"total": 1})),
            ),
            patch("downloader.worker.notify_batch_webhook", new=AsyncMock()) as mock_hook,
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
            "downloader.worker.generate_pdf_from_url",
            new=AsyncMock(return_value=b"%PDF-1.7 worker"),
        ):
            browser_resp = await process_worker_browser_task(
                task, worker_queue=queue, pdf_semaphore=asyncio.Semaphore(2)
            )
        assert browser_resp.success is True
        assert browser_resp.decode_bytes() == b"%PDF-1.7 worker"

    def test_redis_job_store_parses_redis_and_rediss_uris_without_url_kwarg_error(self) -> None:
        from redis.connection import SSLConnection

        from downloader.scheduler.service import _get_redis_job_store

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

    async def test_second_worker_start_does_not_take_inflight_job(self) -> None:
        """A starting worker must not reclaim processing lists of live workers (R12)."""
        redis_stub = FakeRedis()
        q1 = WorkerQueue(redis_stub, worker_id="worker-1")  # type: ignore[arg-type]
        await q1.publish_heartbeat("worker-1", {"active_tasks": 1}, ttl=30)
        await q1.enqueue_batch_job("job-inflight")
        env1 = await q1.pop_batch_job(timeout=1.0)
        assert env1 is not None

        # Starting worker-2 sees worker-1's live heartbeat and leaves job-inflight untouched
        q2 = WorkerQueue(redis_stub, worker_id="worker-2")  # type: ignore[arg-type]
        assert await q2.requeue_processing_jobs() == 0
        assert await q2.pop_batch_job(timeout=0.05) is None
        assert (await q2.get_queue_depths())["batch_processing"] == 1

        # Once worker-1's heartbeat expires/is removed, worker-2 can reclaim it
        await q1.remove_heartbeat("worker-1")
        assert await q2.requeue_processing_jobs() == 1
        reclaimed = await q2.pop_batch_job(timeout=1.0)
        assert reclaimed is not None
        assert reclaimed.job_id == "job-inflight"

    async def test_shutdown_cancel_requeues_instead_of_acking(self) -> None:
        """Worker shutdown cancellation requeues the job with attempt+1 instead of acking (R12)."""
        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub, worker_id="worker-1")  # type: ignore[arg-type]
        await queue.enqueue_batch_job("job-shutdown")
        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None
        assert env.attempt == 1

        mock_jm = MagicMock()
        mock_jm.get_job_info = AsyncMock(
            return_value=JobInfo(
                job_id="job-shutdown",
                status=JobStatus.RUNNING,
                created_at=datetime.now(UTC),
                total_urls=1,
                request_data={"urls": [{"url": "https://example.com"}], "default_format": "text"},
            )
        )
        mock_jm.update_job_status = AsyncMock()
        mock_jm.store_job_results = AsyncMock()

        started = asyncio.Event()
        gate = asyncio.Event()

        async def _slow_batch(*_args, **_kwargs):
            started.set()
            await gate.wait()
            return [], {}

        with patch("downloader.worker.process_background_batch_job", new=_slow_batch):
            task = asyncio.create_task(
                process_worker_batch_envelope(
                    env,
                    job_manager=mock_jm,
                    worker_queue=queue,
                    http_client=MagicMock(),
                    batch_semaphore=asyncio.Semaphore(2),
                    pdf_semaphore=asyncio.Semaphore(1),
                )
            )
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        depths = await queue.get_queue_depths()
        assert depths["batch_processing"] == 0
        assert depths["batch_pending"] == 1
        requeued = await queue.pop_batch_job(timeout=1.0)
        assert requeued is not None
        assert requeued.job_id == "job-shutdown"
        assert requeued.attempt == 2
        # Must NOT have marked the job CANCELLED on worker shutdown
        for call in mock_jm.update_job_status.call_args_list:
            assert call.args[1] != JobStatus.CANCELLED

    async def test_worker_failure_marks_failed_fires_failed_webhook_and_acks(self) -> None:
        """Batch processor failure marks job FAILED, fires failed webhook, and acks (R22)."""
        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub, worker_id="worker-1")  # type: ignore[arg-type]
        await queue.enqueue_batch_job("job-fail")
        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None

        mock_jm = MagicMock()
        mock_jm.get_job_info = AsyncMock(
            return_value=JobInfo(
                job_id="job-fail",
                status=JobStatus.PENDING,
                created_at=datetime.now(UTC),
                total_urls=1,
                request_data={
                    "urls": [{"url": "https://example.com"}],
                    "default_format": "text",
                    "webhook_url": "https://hooks.example.com/fail",
                },
            )
        )
        mock_jm.update_job_status = AsyncMock()
        mock_jm.store_job_results = AsyncMock()

        with (
            patch(
                "downloader.worker.process_background_batch_job",
                new=AsyncMock(side_effect=RuntimeError("boom")),
            ),
            patch("downloader.worker.notify_batch_webhook", new=AsyncMock()) as mock_hook,
        ):
            await process_worker_batch_envelope(
                env,
                job_manager=mock_jm,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(2),
                pdf_semaphore=asyncio.Semaphore(1),
            )

        assert (await queue.get_queue_depths())["batch_processing"] == 0
        assert mock_jm.update_job_status.call_args_list[-1].args[1] == JobStatus.FAILED
        mock_hook.assert_awaited_once()
        assert mock_hook.call_args.args[2] == JobStatus.FAILED

    async def test_worker_skips_missing_and_cancelled_jobs(self) -> None:
        """Worker skips missing or already-terminal jobs and acks their envelopes (R22)."""
        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub, worker_id="worker-1")  # type: ignore[arg-type]
        await queue.enqueue_batch_job("job-missing")
        await queue.enqueue_batch_job("job-cancelled")

        env_missing = await queue.pop_batch_job(timeout=1.0)
        env_cancelled = await queue.pop_batch_job(timeout=1.0)
        assert env_missing is not None and env_cancelled is not None

        mock_jm = MagicMock()
        mock_jm.get_job_info = AsyncMock(
            side_effect=[
                None,
                JobInfo(
                    job_id="job-cancelled",
                    status=JobStatus.CANCELLED,
                    created_at=datetime.now(UTC),
                    request_data={},
                ),
            ]
        )
        mock_jm.update_job_status = AsyncMock()

        with patch("downloader.worker.process_background_batch_job", new=AsyncMock()) as mock_proc:
            await process_worker_batch_envelope(
                env_missing,
                job_manager=mock_jm,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(2),
                pdf_semaphore=asyncio.Semaphore(1),
            )
            await process_worker_batch_envelope(
                env_cancelled,
                job_manager=mock_jm,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(2),
                pdf_semaphore=asyncio.Semaphore(1),
            )
            mock_proc.assert_not_awaited()

        mock_jm.update_job_status.assert_not_awaited()
        assert (await queue.get_queue_depths())["batch_processing"] == 0

    async def test_batch_queue_is_fifo(self) -> None:
        """Batch queue dequeues jobs in FIFO order (R22)."""
        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub)  # type: ignore[arg-type]
        await queue.enqueue_batch_job("job-1")
        await queue.enqueue_batch_job("job-2")
        await queue.enqueue_batch_job("job-3")

        popped = [
            (await queue.pop_batch_job(timeout=1.0)),
            (await queue.pop_batch_job(timeout=1.0)),
            (await queue.pop_batch_job(timeout=1.0)),
        ]
        assert [item.job_id for item in popped if item is not None] == [
            "job-1",
            "job-2",
            "job-3",
        ]

    async def test_remote_browser_calls_are_bounded_and_expired_tasks_skipped(self) -> None:
        """Remote browser calls reject overflow with BrowserCapacityError and skip expired tasks (R38)."""
        redis_stub = FakeRedis()
        blpop_started = asyncio.Event()
        release_blpop = asyncio.Event()
        original_blpop = redis_stub.blpop

        async def _blocking_blpop(keys: list[str], timeout: float = 0):
            blpop_started.set()
            await release_blpop.wait()
            return await original_blpop(keys, timeout=timeout)

        redis_stub.blpop = _blocking_blpop  # type: ignore[method-assign]
        queue = WorkerQueue(redis_stub, max_remote_browser_calls=1)  # type: ignore[arg-type]

        req1 = BrowserTaskRequest(kind="render_html", url="https://example.com/1")
        await queue.publish_browser_result(
            BrowserTaskResponse(
                task_id=req1.task_id, success=True, content_base64="PGh0bWw+PC9odG1sPg=="
            )
        )
        t1 = asyncio.create_task(queue.enqueue_browser_task(req1, timeout=5.0))
        await blpop_started.wait()

        req2 = BrowserTaskRequest(kind="render_html", url="https://example.com/2")
        with pytest.raises(BrowserCapacityError):
            await queue.enqueue_browser_task(req2, timeout=5.0)

        release_blpop.set()
        resp1 = await t1
        assert resp1.success is True

        # Expired task must be skipped by worker without invoking Playwright
        expired_req = BrowserTaskRequest(
            kind="pdf",
            url="https://example.com/expired",
            deadline=1.0,
        )
        with patch("downloader.worker.generate_pdf_from_url", new=AsyncMock()) as mock_pdf:
            expired_resp = await process_worker_browser_task(
                expired_req,
                worker_queue=queue,
                pdf_semaphore=asyncio.Semaphore(1),
            )
            mock_pdf.assert_not_awaited()
        assert expired_resp.success is False
        assert expired_resp.status_code == 408
        assert expired_resp.error_type == "timeout_error"


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

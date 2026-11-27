"""Offline unit tests for v0.7.0 graceful drain, split worker architecture, and OpenTelemetry."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
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
            async with app.router.lifespan_context(app):
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
                app.state.drain_coordinator.reset()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    async def test_lifespan_shutdown_drains_before_closing_clients(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from downloader.main import _teardown_services

        events: list[str] = []
        coord = DrainCoordinator()
        orig_start = coord.start_drain
        orig_wait = coord.wait_until_drained

        def _tracked_start() -> None:
            events.append("start_drain")
            orig_start()

        async def _tracked_wait(timeout: float) -> bool:
            events.append("wait_until_drained")
            return await orig_wait(timeout)

        monkeypatch.setattr(coord, "start_drain", _tracked_start)
        monkeypatch.setattr(coord, "wait_until_drained", _tracked_wait)

        fake_http = AsyncMock()
        fake_http.close = AsyncMock(side_effect=lambda: events.append("http_client.close"))
        fake_robots = AsyncMock()
        fake_robots.close = AsyncMock(side_effect=lambda: events.append("robots_checker.close"))
        fake_jm = AsyncMock()
        fake_jm.wait_for_active_jobs = AsyncMock(
            side_effect=lambda _t: events.append("wait_for_active_jobs")
        )
        fake_jm.disconnect = AsyncMock(side_effect=lambda: events.append("job_manager.disconnect"))
        fake_wq = AsyncMock()
        fake_wq.close = AsyncMock(side_effect=lambda: events.append("worker_queue.close"))
        fake_metrics = AsyncMock()
        fake_metrics.stop = AsyncMock(side_effect=lambda: events.append("metrics.stop"))

        fake_app = MagicMock()
        fake_app.state.job_manager = fake_jm
        fake_app.state.worker_queue = fake_wq
        fake_app.state.scheduler = None
        fake_app.state.robots_checker = fake_robots
        fake_app.state.pdf_generator = None

        await _teardown_services(fake_app, Settings(), coord, fake_metrics, fake_http)
        assert coord.is_draining is False
        assert events[:2] == ["start_drain", "wait_until_drained"]
        assert events.index("wait_until_drained") < events.index("wait_for_active_jobs")
        assert events.index("wait_for_active_jobs") < events.index("http_client.close")
        assert events.index("job_manager.disconnect") < events.index("http_client.close")
        assert events.index("wait_until_drained") < events.index("robots_checker.close")
        assert "worker_queue.close" in events

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

    async def test_health_503_is_observable_for_a_probe_period_after_sigterm(self) -> None:
        """SIGTERM holds `/health` at 503 for `pre_stop_delay` before chaining server stop (`R100`)."""
        coord = DrainCoordinator()
        chained_at: list[float] = []

        def _prev(_signum: int, _frame: object) -> None:
            chained_at.append(time.monotonic())

        async with app.router.lifespan_context(app):
            orig_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, _prev)
            prev_coord = app.state.drain_coordinator
            app.state.drain_coordinator = coord
            try:
                restore = install_sigterm_drain_handler(coord, pre_stop_delay=0.15)
                try:
                    sent_at = time.monotonic()
                    os.kill(os.getpid(), signal.SIGTERM)
                    # Immediately enters drain mode while server shutdown has NOT been chained yet.
                    assert coord.is_draining is True
                    assert chained_at == []

                    transport = httpx.ASGITransport(app=app)
                    async with httpx.AsyncClient(
                        transport=transport, base_url="http://test"
                    ) as client:
                        probe = await client.get("/health")
                        assert probe.status_code == 503
                        assert probe.json()["status"] == "draining"
                        assert chained_at == []

                    await asyncio.sleep(0.2)
                    assert len(chained_at) == 1
                    assert (chained_at[0] - sent_at) >= 0.12
                finally:
                    restore()
            finally:
                app.state.drain_coordinator = prev_coord
                signal.signal(signal.SIGTERM, orig_handler)

    @pytest.mark.xdist_group("uvicorn_drain")
    def test_uvicorn_subprocess_enters_drain_on_sigterm(self) -> None:
        from pathlib import Path

        repo_root = Path(__file__).resolve().parents[2]
        env = {
            **os.environ,
            "ENVIRONMENT": "development",
            "REDIS_URI": "",
            "OTEL_ENABLED": "false",
            "SHUTDOWN_PRE_STOP_DELAY": "0.1",
            "PYTHONUNBUFFERED": "1",
        }
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "--app-dir",
                str(repo_root / "src"),
                "downloader.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--log-level",
                "info",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        captured_lines: list[str] = []
        try:
            assert proc.stdout is not None
            ready = False
            deadline = time.monotonic() + 20.0
            import select

            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                rlist, _, _ = select.select([proc.stdout], [], [], 0.1)
                if rlist:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    captured_lines.append(line)
                    if "Uvicorn running on" in line:
                        ready = True
                        break
            if not ready:
                proc.terminate()
                tail, _ = proc.communicate(timeout=5.0)
                pytest.fail(
                    f"Uvicorn subprocess did not report ready (rc={proc.returncode}):\n"
                    f"{''.join(captured_lines)}{tail}"
                )

            proc.send_signal(signal.SIGTERM)
            tail, _ = proc.communicate(timeout=15.0)
            output = "".join(captured_lines) + tail
            assert proc.returncode in (0, -signal.SIGTERM), output
            assert "Entering connection drain mode" in output
            assert "All services shut down successfully" in output
            assert "Finished server process" in output
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5.0)


async def _read_resp2_command(reader: asyncio.StreamReader) -> list[str] | None:
    header = await reader.readline()
    if not header:
        return None
    if not header.startswith(b"*"):
        return []
    argc = int(header[1:].strip())
    args: list[str] = []
    for _ in range(argc):
        bulk_len = int((await reader.readline())[1:].strip())
        data = await reader.readexactly(bulk_len)
        await reader.readexactly(2)
        args.append(data.decode("utf-8", errors="replace"))
    return args


def _encode_resp2_blpop_reply(key: str, html_bytes: bytes) -> bytes:
    import base64

    payload = BrowserTaskResponse(
        task_id=key.removeprefix("browser_result:"),
        success=True,
        content_base64=base64.b64encode(html_bytes).decode("ascii"),
        status_code=200,
    ).model_dump_json()
    key_b = key.encode("utf-8")
    val_b = payload.encode("utf-8")
    return (
        f"*2\r\n${len(key_b)}\r\n".encode("ascii")
        + key_b
        + f"\r\n${len(val_b)}\r\n".encode("ascii")
        + val_b
        + b"\r\n"
    )


class TestSplitWorkerArchitecture:
    async def test_worker_queue_batch_browser_and_heartbeat_operations(self) -> None:
        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub)

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
        assert env2.attempt == 2
        await queue.ack_batch_job(env2.raw_payload)
        assert (await queue.get_queue_depths())["batch_processing"] == 0

        # Heartbeat publish -> has_heartbeat -> remove
        await queue.publish_heartbeat("worker-1", {"active_tasks": 2}, ttl=30)
        assert await queue.has_heartbeat("worker-1") is True
        await queue.remove_heartbeat("worker-1")
        assert await queue.has_heartbeat("worker-1") is False

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
                app.state.worker_queue = WorkerQueue(redis_stub)

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
        queue = WorkerQueue(redis_stub)
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
        mock_jm.update_job_status = AsyncMock(return_value=True)
        mock_jm.complete_job = AsyncMock(return_value=True)

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
        mock_jm.complete_job.assert_awaited_once()
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
        q1 = WorkerQueue(redis_stub, worker_id="worker-1")
        await q1.publish_heartbeat("worker-1", {"active_tasks": 1}, ttl=30)
        await q1.enqueue_batch_job("job-inflight")
        env1 = await q1.pop_batch_job(timeout=1.0)
        assert env1 is not None

        # Starting worker-2 sees worker-1's live heartbeat and leaves job-inflight untouched
        q2 = WorkerQueue(redis_stub, worker_id="worker-2")
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
        queue = WorkerQueue(redis_stub, worker_id="worker-1")
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
        mock_jm.update_job_status = AsyncMock(return_value=True)
        mock_jm.complete_job = AsyncMock(return_value=True)

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
        # Requeueing an entry already removed/reclaimed must return False and not duplicate (`R113`).
        assert await queue.requeue_batch_job(env) is False
        # Must NOT have marked the job CANCELLED on worker shutdown
        for call in mock_jm.update_job_status.call_args_list:
            assert call.args[1] != JobStatus.CANCELLED

    async def test_worker_failure_marks_failed_fires_failed_webhook_and_acks(self) -> None:
        """Batch processor failure marks job FAILED, fires failed webhook even if status write errors (`R22`, `R113`)."""
        import redis.exceptions as redis_exc

        redis_stub = FakeRedis()
        queue = WorkerQueue(redis_stub, worker_id="worker-1")
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
        mock_jm.update_job_status = AsyncMock(
            side_effect=[True, redis_exc.ConnectionError("transient redis write error")]
        )
        mock_jm.complete_job = AsyncMock(return_value=True)

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
        queue = WorkerQueue(redis_stub, worker_id="worker-1")
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
        queue = WorkerQueue(redis_stub)
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

    async def test_draining_worker_keeps_heartbeat_until_jobs_finish(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Draining worker keeps publishing heartbeats until active jobs finish (`R83`, `R113`)."""
        from pydantic import ValidationError

        from downloader.config import ShutdownConfig, WorkerConfig
        from downloader.worker import _WorkerRuntime, run_worker
        from tests.fixtures.in_memory_ports import InMemoryJobStore, InMemoryWorkQueue

        # Config validation rejects heartbeat_ttl < drain_timeout + heartbeat_interval and allows 600s
        with pytest.raises(ValidationError, match="heartbeat_ttl"):
            Settings(
                shutdown=ShutdownConfig(drain_timeout=30.0),
                worker=WorkerConfig(heartbeat_interval=10.0, heartbeat_ttl=30),
            )
        wide_cfg = Settings(
            shutdown=ShutdownConfig(drain_timeout=300.0),
            worker=WorkerConfig(heartbeat_interval=10.0, heartbeat_ttl=320),
        )
        assert wide_cfg.worker.heartbeat_ttl == 320

        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "drain-worker.id"))
        store = InMemoryJobStore()
        queue = InMemoryWorkQueue()
        job_id = await store.create_job(
            {"urls": [{"url": "https://example.com"}], "default_format": "text"}
        )
        await queue.enqueue_batch_job(job_id)

        job_started = asyncio.Event()
        release_job = asyncio.Event()
        heartbeats_during_drain = 0
        stop_event = asyncio.Event()

        orig_publish = queue.publish_heartbeat

        async def _spy_heartbeat(wid: str, payload: dict[str, object], ttl: int = 30) -> None:
            nonlocal heartbeats_during_drain
            if stop_event.is_set() and job_started.is_set() and not release_job.is_set():
                heartbeats_during_drain += 1
            await orig_publish(wid, payload, ttl=ttl)

        monkeypatch.setattr(queue, "publish_heartbeat", _spy_heartbeat)

        async def _slow_batch(*_args, **_kwargs):
            job_started.set()
            await release_job.wait()
            return [{"url": "https://example.com", "success": True}], {
                "total_requests": 1,
                "successful_requests": 1,
                "failed_requests": 0,
                "total_duration": 0.05,
            }

        cfg = Settings(
            shutdown=ShutdownConfig(drain_timeout=5.0),
            worker=WorkerConfig(
                heartbeat_interval=1.0,
                heartbeat_ttl=10,
                poll_timeout=1.0,
                remote_browser_enabled=False,
            ),
        )
        # Speed up heartbeat ticks for deterministic test runtime
        object.__setattr__(cfg.worker, "heartbeat_interval", 0.05)
        rt = _WorkerRuntime(
            worker_id="draining-w1",
            settings=cfg,
            http_client=AsyncMock(close=AsyncMock()),
            pdf_generator=AsyncMock(close=AsyncMock(), available_browsers=1),
            pdf_semaphore=asyncio.Semaphore(1),
            batch_semaphore=asyncio.Semaphore(2),
            job_slot_semaphore=asyncio.Semaphore(1),
            browser_slot_semaphore=asyncio.Semaphore(1),
            job_manager=store,
            worker_queue=queue,
            robots_checker=AsyncMock(close=AsyncMock()),
            domain_throttle=MagicMock(),
            scheduler=None,
            renderer=MagicMock(),
        )

        with patch("downloader.worker.process_background_batch_job", new=_slow_batch):
            worker_task = asyncio.create_task(
                run_worker(settings=cfg, runtime=rt, stop_event=stop_event)
            )
            await asyncio.wait_for(job_started.wait(), timeout=2.0)
            # Trigger drain while job is still running; heartbeat must stay alive and keep ticking!
            stop_event.set()
            await asyncio.sleep(0.15)
            assert await queue.has_heartbeat("draining-w1") is True
            assert heartbeats_during_drain >= 1

            release_job.set()
            await asyncio.wait_for(worker_task, timeout=3.0)

        # Heartbeat is removed only after drain finishes.
        assert await queue.has_heartbeat("draining-w1") is False

    async def test_worker_result_blob_reports_completed(self) -> None:
        """Worker persists result blob with `status == COMPLETED` and skips cancelled jobs (`R84`)."""
        from tests.fixtures.in_memory_ports import InMemoryJobStore, InMemoryWorkQueue

        store = InMemoryJobStore()
        queue = InMemoryWorkQueue()

        # 1. Normal completion stores COMPLETED JobResult blob
        job_id = await store.create_job(
            {"urls": [{"url": "https://example.com"}], "default_format": "text"}
        )
        await queue.enqueue_batch_job(job_id)
        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None

        with (
            patch(
                "downloader.worker.process_background_batch_job",
                new=AsyncMock(
                    return_value=(
                        [{"url": "https://example.com", "success": True}],
                        {
                            "total_requests": 1,
                            "successful_requests": 1,
                            "failed_requests": 0,
                            "total_duration": 0.25,
                        },
                    )
                ),
            ),
            patch("downloader.worker.notify_batch_webhook", new=AsyncMock()),
        ):
            await process_worker_batch_envelope(
                env,
                job_manager=store,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(2),
                pdf_semaphore=asyncio.Semaphore(1),
            )

        stored_result = await store.get_job_results(job_id)
        assert stored_result is not None
        assert stored_result.status == JobStatus.COMPLETED
        assert stored_result.total_duration == pytest.approx(0.25)

        # 2. Job cancelled mid-flight stores NO result blob and fires NO completed webhook
        cancel_id = await store.create_job(
            {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
                "webhook_url": "https://hooks.example.com/batch",
            }
        )
        await queue.enqueue_batch_job(cancel_id)
        env_c = await queue.pop_batch_job(timeout=1.0)
        assert env_c is not None

        async def _cancel_mid_run(jid: str, *_args, **_kwargs):
            await store.update_job_status(jid, JobStatus.CANCELLED)
            return [{"url": "https://example.com", "success": True}], {"total_requests": 1}

        with (
            patch("downloader.worker.process_background_batch_job", new=_cancel_mid_run),
            patch("downloader.worker.notify_batch_webhook", new=AsyncMock()) as mock_hook,
        ):
            await process_worker_batch_envelope(
                env_c,
                job_manager=store,
                worker_queue=queue,
                http_client=MagicMock(),
                batch_semaphore=asyncio.Semaphore(2),
                pdf_semaphore=asyncio.Semaphore(1),
            )

        assert await store.get_job_results(cancel_id) is None
        mock_hook.assert_not_awaited()

    async def test_reclaimed_job_increments_attempt_and_fails_after_max(self) -> None:
        """Reclaim increments attempt atomically and dead-letters jobs exceeding `max_attempts` (`R86`)."""
        redis_stub = FakeRedis()
        q1 = WorkerQueue(redis_stub, worker_id="w-dead", max_attempts=2)
        q2 = WorkerQueue(redis_stub, worker_id="w-reclaimer", max_attempts=2)

        await q1.enqueue_batch_job("job-poison")
        # Attempt 1 -> worker dies without heartbeat
        env1 = await q1.pop_batch_job(timeout=1.0)
        assert env1 is not None and env1.attempt == 1

        dead_letters: list[str] = []

        async def _on_dlq(env) -> None:
            dead_letters.append(env.job_id)

        # Reclaim #1: attempt 1 -> 2 (<= max_attempts=2), requeued to pending
        moved1 = await q2.requeue_processing_jobs(max_attempts=2, on_dead_letter=_on_dlq)
        assert moved1 == 1
        assert dead_letters == []
        env2 = await q1.pop_batch_job(timeout=1.0)
        assert env2 is not None and env2.attempt == 2

        # Reclaim #2: attempt 2 -> 3 (> max_attempts=2), routed to dead-letter callback & NOT requeued
        moved2 = await q2.requeue_processing_jobs(max_attempts=2, on_dead_letter=_on_dlq)
        assert moved2 == 0
        assert dead_letters == ["job-poison"]
        assert await q2.pop_batch_job(timeout=0.05) is None
        assert (await q2.get_queue_depths())["batch_processing"] == 0

    async def test_periodic_reclaim_loop_skips_own_inflight_batch_jobs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Periodic `_run_reclaim_loop` never reclaims or dead-letters this worker's own in-flight batch jobs (`S-1`)."""
        from collections.abc import Awaitable
        from typing import Any

        from downloader.config import WorkerConfig
        from downloader.worker import _fail_dead_letter_batch_job, _run_reclaim_loop, _WorkerRuntime
        from tests.fixtures.in_memory_ports import InMemoryJobStore

        redis_stub = FakeRedis()
        store = InMemoryJobStore()
        queue = WorkerQueue(redis_stub, worker_id="w-live", max_attempts=2)
        await queue.publish_heartbeat("w-live", {"active_tasks": 1}, ttl=60)

        job_id = await store.create_job(
            {"urls": [{"url": "https://example.com"}], "default_format": "text"}
        )
        await queue.enqueue_batch_job(job_id)
        env = await queue.pop_batch_job(timeout=1.0)
        assert env is not None

        job_started = asyncio.Event()
        finish_job = asyncio.Event()

        async def _long_running_batch(
            *_args: object, **_kwargs: object
        ) -> tuple[list[dict[str, object]], dict[str, object]]:
            job_started.set()
            await finish_job.wait()
            return [{"url": "https://example.com", "success": True}], {
                "total_requests": 1,
                "successful_requests": 1,
                "failed_requests": 0,
                "total_duration": 0.2,
            }

        reclaim_stop = asyncio.Event()
        ticks = 0
        orig_wait_for = asyncio.wait_for

        async def _fast_reclaim_wait(coro: Awaitable[Any], timeout: float | None = None) -> Any:
            nonlocal ticks
            ticks += 1
            if ticks > 3:
                reclaim_stop.set()
            return await orig_wait_for(coro, timeout=0.01)

        cfg = Settings(worker=WorkerConfig(heartbeat_interval=1.0, max_attempts=2))
        rt = _WorkerRuntime(
            worker_id="w-live",
            settings=cfg,
            http_client=MagicMock(),
            pdf_generator=MagicMock(),
            pdf_semaphore=asyncio.Semaphore(1),
            batch_semaphore=asyncio.Semaphore(1),
            job_slot_semaphore=asyncio.Semaphore(1),
            browser_slot_semaphore=asyncio.Semaphore(1),
            job_manager=store,
            worker_queue=queue,
            robots_checker=MagicMock(),
            domain_throttle=MagicMock(),
            scheduler=None,
            renderer=MagicMock(),
        )

        with (
            patch("downloader.worker.process_background_batch_job", new=_long_running_batch),
            patch("downloader.worker.notify_batch_webhook", new=AsyncMock()),
        ):
            batch_task = asyncio.create_task(
                process_worker_batch_envelope(
                    env,
                    job_manager=store,
                    worker_queue=queue,
                    http_client=MagicMock(),
                    batch_semaphore=asyncio.Semaphore(1),
                    pdf_semaphore=asyncio.Semaphore(1),
                    settings=cfg,
                )
            )
            await job_started.wait()
            monkeypatch.setattr("downloader.worker.asyncio.wait_for", _fast_reclaim_wait)
            await _run_reclaim_loop(rt, reclaim_stop, logging.getLogger("test"))
            monkeypatch.undo()

            assert ticks >= 4
            depths = await queue.get_queue_depths()
            assert depths["batch_pending"] == 0
            assert depths["batch_processing"] == 1
            running_info = await store.get_job_info(job_id)
            assert running_info is not None and running_info.status == JobStatus.RUNNING

            finish_job.set()
            await batch_task

        done_info = await store.get_job_info(job_id)
        assert done_info is not None and done_info.status == JobStatus.COMPLETED
        assert (await store.get_job_results(job_id)) is not None
        assert (await queue.get_queue_depths())["batch_processing"] == 0

        # Crash recovery at start-up still reclaims own leftover list even if stale heartbeat exists
        await queue.enqueue_batch_job("job-crash-leftover")
        orphaned = await queue.pop_batch_job(timeout=1.0)
        assert orphaned is not None
        assert (
            await queue.requeue_processing_jobs(
                max_attempts=2,
                on_dead_letter=lambda dl: _fail_dead_letter_batch_job(
                    dl, job_manager=store, settings=cfg
                ),
            )
            == 1
        )

    async def test_remote_browser_pool_socket_timeout_and_redis_timeout_maps_to_504(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Browser pool uses socket_timeout above task timeout and maps Redis timeouts to 504 `worker_timeout` (`S-2`)."""
        from downloader.browser.renderer import RemoteBrowserRenderer
        from downloader.errors import classify_domain_error
        from downloader.ports import WorkerTimeoutError

        monkeypatch.setattr("downloader.job_manager._REDIS_SOCKET_TIMEOUT_SECONDS", 0.15)
        hang_blpop = False
        static_replies = {
            "PING": b"+PONG\r\n",
            "HELLO": b"-ERR unsupported in stub\r\n",
            "CLIENT": b"-ERR unsupported in stub\r\n",
            "LPUSH": b":1\r\n",
        }

        async def _resp2_stub(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while (args := await _read_resp2_command(reader)) is not None:
                    cmd = args[0].upper() if args else ""
                    if not cmd:
                        continue
                    if cmd != "BLPOP":
                        writer.write(static_replies.get(cmd, b"+OK\r\n"))
                    elif hang_blpop:
                        await asyncio.sleep(5.0)
                        break
                    else:
                        await asyncio.sleep(0.40)
                        writer.write(_encode_resp2_blpop_reply(args[1], b"<html>ok</html>"))
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(_resp2_stub, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        jm = JobManager(f"redis://127.0.0.1:{port}/0", max_connections=4)
        await jm.connect()
        queue = WorkerQueue(jm.redis_client)
        try:
            renderer = RemoteBrowserRenderer(queue, timeout=1.5)
            assert await renderer.render_html("https://example.com") == b"<html>ok</html>"

            long_renderer = RemoteBrowserRenderer(queue, timeout=60.0)
            assert await long_renderer.render_html("https://example.com") == b"<html>ok</html>"
            assert queue._browser_pool is not None
            assert float(queue._browser_pool.connection_kwargs["socket_timeout"]) >= 65.0

            hang_blpop = True
            queue._browser_pool.connection_kwargs["socket_timeout"] = 0.15
            await queue._browser_pool.disconnect()
            queue._browser_pool.reset()
            with pytest.raises(WorkerTimeoutError) as exc_info:
                await renderer.render_html("https://example.com/hung")
            classified = classify_domain_error(exc_info.value, context="render")
            assert classified.status_code == 504
            assert classified.error_type == "worker_timeout"
        finally:
            await queue.close()
            await jm.disconnect()
            server.close()
            await server.wait_closed()

    async def test_remote_browser_calls_are_bounded_and_expired_tasks_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Remote browser calls reject overflow, skip tasks expiring behind a held semaphore, and validate actions safely (`R38`, `R87`)."""
        redis_stub = FakeRedis()
        blpop_started = asyncio.Event()
        release_blpop = asyncio.Event()
        original_blpop = redis_stub.blpop

        async def _blocking_blpop(keys: list[str], timeout: float = 0):
            blpop_started.set()
            await release_blpop.wait()
            return await original_blpop(keys, timeout=timeout)

        monkeypatch.setattr(redis_stub, "blpop", _blocking_blpop)
        queue = WorkerQueue(redis_stub, max_remote_browser_calls=1)

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

        # 1. Already-expired task is skipped before touching semaphore or Playwright
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

        # 2. Task that expires WHILE waiting for a held pdf_semaphore must also be skipped (`R87`)
        held_sem = asyncio.Semaphore(1)
        await held_sem.acquire()
        short_req = BrowserTaskRequest(
            kind="pdf",
            url="https://example.com/expires-in-queue",
            deadline=time.time() + 0.05,
        )
        with patch("downloader.worker.generate_pdf_from_url", new=AsyncMock()) as mock_pdf2:
            waiting = asyncio.create_task(
                process_worker_browser_task(
                    short_req,
                    worker_queue=queue,
                    pdf_semaphore=held_sem,
                )
            )
            await asyncio.sleep(0.08)
            held_sem.release()
            queued_expired = await waiting
            mock_pdf2.assert_not_awaited()
        assert queued_expired.success is False
        assert queued_expired.status_code == 408
        assert queued_expired.error_type == "timeout_error"

        # 3. Malformed actions inside BrowserTaskRequest are serialized as 400 browser_action_error
        bad_action_req = BrowserTaskRequest(
            kind="render_html",
            url="https://example.com/bad-actions",
            actions=[{"type": "evaluate", "script": "alert(1)"}],
        )
        bad_resp = await process_worker_browser_task(
            bad_action_req,
            worker_queue=queue,
            pdf_semaphore=asyncio.Semaphore(1),
        )
        assert bad_resp.success is False
        assert bad_resp.status_code == 400
        assert bad_resp.error_type == "browser_action_error"

    async def test_run_worker_end_to_end_with_in_memory_ports_and_metrics(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Full `run_worker()` drives batch + browser loops, updates metrics, and serves `/metrics` (`R80`, `R104`)."""
        from downloader.config import ShutdownConfig, WorkerConfig
        from downloader.metrics import get_metrics_collector
        from downloader.worker import _start_worker_metrics_server, _WorkerRuntime, run_worker
        from tests.fixtures.in_memory_ports import InMemoryJobStore, InMemoryWorkQueue

        monkeypatch.setenv("WORKER_ID_FILE", str(tmp_path / "e2e-worker.id"))
        store = InMemoryJobStore()
        queue = InMemoryWorkQueue()
        job_id = await store.create_job(
            {"urls": [{"url": "https://example.com"}], "default_format": "text"}
        )
        await queue.enqueue_batch_job(job_id)

        browser_req = BrowserTaskRequest(kind="pdf", url="https://example.com/doc")
        metrics_server = await _start_worker_metrics_server(0)
        port = metrics_server.sockets[0].getsockname()[1]

        cfg = Settings(
            shutdown=ShutdownConfig(drain_timeout=5.0),
            worker=WorkerConfig(
                heartbeat_interval=1.0,
                heartbeat_ttl=10,
                poll_timeout=1.0,
                remote_browser_enabled=True,
            ),
        )
        stop_event = asyncio.Event()
        rt = _WorkerRuntime(
            worker_id="e2e-w1",
            settings=cfg,
            http_client=AsyncMock(close=AsyncMock()),
            pdf_generator=AsyncMock(close=AsyncMock(), available_browsers=2),
            pdf_semaphore=asyncio.Semaphore(2),
            batch_semaphore=asyncio.Semaphore(2),
            job_slot_semaphore=asyncio.Semaphore(2),
            browser_slot_semaphore=asyncio.Semaphore(2),
            job_manager=store,
            worker_queue=queue,
            robots_checker=AsyncMock(close=AsyncMock()),
            domain_throttle=MagicMock(),
            scheduler=None,
            renderer=MagicMock(),
            metrics_server=metrics_server,
        )

        with (
            patch(
                "downloader.worker.process_background_batch_job",
                new=AsyncMock(
                    return_value=(
                        [{"url": "https://example.com", "success": True}],
                        {
                            "total_requests": 1,
                            "successful_requests": 1,
                            "failed_requests": 0,
                            "total_duration": 0.1,
                        },
                    )
                ),
            ),
            patch(
                "downloader.worker.generate_pdf_from_url",
                new=AsyncMock(return_value=b"%PDF-1.7 live"),
            ),
        ):
            worker_task = asyncio.create_task(
                run_worker(settings=cfg, runtime=rt, stop_event=stop_event)
            )
            rpc_resp = await queue.enqueue_browser_task(browser_req, timeout=2.0)
            assert rpc_resp.success is True
            assert rpc_resp.decode_bytes() == b"%PDF-1.7 live"

            # Wait briefly for batch job completion
            for _ in range(40):
                info = await store.get_job_info(job_id)
                if info is not None and info.status == JobStatus.COMPLETED:
                    break
                await asyncio.sleep(0.02)
            final_info = await store.get_job_info(job_id)
            assert final_info is not None and final_info.status == JobStatus.COMPLETED

            # Scrape the worker's Prometheus metrics HTTP server
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"GET /metrics HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            raw_metrics = (await reader.read()).decode("utf-8")
            writer.close()
            await writer.wait_closed()
            assert "HTTP/1.1 200 OK" in raw_metrics
            assert "worker_active_tasks" in raw_metrics
            assert get_metrics_collector()._gauges.get("pdf_pool_available") == 2.0

            stop_event.set()
            await asyncio.wait_for(worker_task, timeout=3.0)

    async def test_worker_dead_letter_reclaim_error_serialization_and_cli_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cover dead-letter handling, reclaim loop, browser error serialization, metrics 404, and CLI exit (`R80`)."""
        from typing import Any

        from downloader.config import WorkerConfig
        from downloader.models.browser_actions import BrowserActionError
        from downloader.ports import (
            BatchQueueEnvelope,
            BrowserCapacityError,
            ContentConversionError,
            ContentTooLargeError,
            CrawlDelayExceededError,
            PDFGeneratorError,
            RenderTimeoutError,
            RobotsDisallowedError,
            ScreenshotGeneratorError,
            SelectorTimeoutError,
        )
        from downloader.services.webhooks import seal_webhook_secret
        from downloader.validation import SSRFProtectionError, URLValidationError
        from downloader.worker import (
            _decode_batch_request,
            _fail_dead_letter_batch_job,
            _run_reclaim_loop,
            _serialize_browser_task_error,
            _start_worker_metrics_server,
            _WorkerRuntime,
            run_worker,
        )
        from downloader.worker import main as worker_main

        # 1. _decode_batch_request decrypts sealed webhook secret and handles legacy cleartext
        cfg = Settings()
        cfg.webhook.secret_key = "worker-test-secret-key"  # pragma: allowlist secret
        sealed = seal_webhook_secret("hook-secret-123", cfg)
        decoded_sealed = _decode_batch_request(
            {
                "urls": [{"url": "https://example.com"}],
                "webhook_url": "https://example.com/hook",
                "webhook_secret_enc": sealed,
            },
            cfg,
        )
        assert decoded_sealed.webhook_secret == "hook-secret-123"
        decoded_legacy = _decode_batch_request(
            {
                "urls": [{"url": "https://example.com"}],
                "webhook_url": "https://example.com/hook",
                "webhook_secret": "plain-legacy",
            },
            cfg,
        )
        assert decoded_legacy.webhook_secret == "plain-legacy"

        # 2. _fail_dead_letter_batch_job marks job FAILED and delivers webhook
        mock_jm = MagicMock()
        mock_jm.get_job_info = AsyncMock(
            return_value=JobInfo(
                job_id="dl-1",
                status=JobStatus.RUNNING,
                created_at=datetime.now(UTC),
                total_urls=1,
                request_data={
                    "urls": [{"url": "https://example.com"}],
                    "webhook_url": "https://example.com/hook",
                    "webhook_secret_enc": sealed,
                },
            )
        )
        mock_jm.update_job_status = AsyncMock(return_value=True)
        env = BatchQueueEnvelope(job_id="dl-1", attempt=4, trace_headers={}, raw_payload="{}")
        with patch("downloader.worker.notify_batch_webhook", new=AsyncMock()) as mock_hook:
            await _fail_dead_letter_batch_job(env, job_manager=mock_jm, settings=cfg)
        mock_jm.update_job_status.assert_awaited_once()
        mock_hook.assert_awaited_once()

        # 3. _serialize_browser_task_error maps every domain exception cleanly
        task = BrowserTaskRequest(kind="render_html", url="https://example.com")
        cases = [
            (SSRFProtectionError("ssrf"), 400, "ssrf_protection_error"),
            (URLValidationError("bad"), 400, "validation_error"),
            (RobotsDisallowedError("https://example.com"), 403, "robots_disallowed"),
            (CrawlDelayExceededError("https://example.com", 2.0), 429, "crawl_delay_exceeded"),
            (SelectorTimeoutError("#x", 500), 408, "selector_timeout_error"),
            (BrowserActionError("bad action"), 400, "browser_action_error"),
            (ContentTooLargeError(200, 100), 413, "content_too_large"),
            (ContentConversionError("conv"), 500, "conversion_error"),
            (BrowserCapacityError("full"), 503, "service_unavailable"),
            (RenderTimeoutError("slow"), 408, "timeout_error"),
            (ScreenshotGeneratorError("shot", status_code=502), 502, "screenshot_generation_error"),
            (PDFGeneratorError("pdf", status_code=404), 404, "http_error"),
            (PDFGeneratorError("pdf crash"), 500, "pdf_generation_error"),
            (RuntimeError("unexpected"), 500, "render_error"),
        ]
        for exc, expected_code, expected_type in cases:
            resp = _serialize_browser_task_error(task, exc)
            assert resp.success is False
            assert resp.status_code == expected_code
            assert resp.error_type == expected_type

        # 4. _start_worker_metrics_server serves 404 on unknown paths
        server = await _start_worker_metrics_server(0)
        try:
            assert server.sockets
            mport = server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", mport)
            writer.write(b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            raw_404 = (await reader.read()).decode("utf-8")
            writer.close()
            await writer.wait_closed()
            assert "404 Not Found" in raw_404
        finally:
            server.close()
            await server.wait_closed()

        # 5. _run_reclaim_loop reclaims orphaned jobs on interval tick
        reclaim_stop = asyncio.Event()
        mock_wq = MagicMock()
        reclaim_calls = 0

        async def _requeue(**_kwargs):
            nonlocal reclaim_calls
            reclaim_calls += 1
            reclaim_stop.set()
            return 2

        mock_wq.requeue_processing_jobs = AsyncMock(side_effect=_requeue)
        reclaim_rt = _WorkerRuntime(
            worker_id="reclaim-w",
            settings=Settings(worker=WorkerConfig(heartbeat_interval=1.0)),
            http_client=MagicMock(),
            pdf_generator=MagicMock(),
            pdf_semaphore=asyncio.Semaphore(1),
            batch_semaphore=asyncio.Semaphore(1),
            job_slot_semaphore=asyncio.Semaphore(1),
            browser_slot_semaphore=asyncio.Semaphore(1),
            job_manager=mock_jm,
            worker_queue=mock_wq,
            robots_checker=MagicMock(),
            domain_throttle=MagicMock(),
            scheduler=None,
            renderer=MagicMock(),
        )
        orig_wait_for = asyncio.wait_for

        async def _fast_wait_for(coro, timeout=None):
            return await orig_wait_for(coro, timeout=min(timeout or 0.01, 0.01))

        monkeypatch.setattr("downloader.worker.asyncio.wait_for", _fast_wait_for)
        await _run_reclaim_loop(reclaim_rt, reclaim_stop, logging.getLogger("test"))
        assert reclaim_calls == 1

        # 6. run_worker without REDIS_URI exits with status 1
        no_redis_cfg = Settings()
        no_redis_cfg.redis.redis_uri = None
        with pytest.raises(SystemExit) as exc_info:
            await run_worker(settings=no_redis_cfg)
        assert exc_info.value.code == 1

        # 7. main() --healthcheck dispatches run_healthcheck
        monkeypatch.setattr(sys, "argv", ["worker", "--healthcheck"])

        def _fake_asyncio_run(coro: Any) -> int:
            coro.close()
            return 0

        with (
            patch("downloader.worker.asyncio.run", side_effect=_fake_asyncio_run) as mock_run,
            pytest.raises(SystemExit) as hc_exit,
        ):
            worker_main()
        assert hc_exit.value.code == 0
        mock_run.assert_called_once()

    async def test_init_worker_runtime_and_dependency_providers(self) -> None:
        """Cover `_init_worker_runtime` assembly and lazy/error paths in `dependencies.py` (`R80`)."""
        from types import SimpleNamespace
        from typing import cast

        from fastapi import Request

        from downloader.browser.renderer import RemoteBrowserRenderer
        from downloader.dependencies import (
            build_browser_renderer,
            get_batch_semaphore,
            get_domain_throttle,
            get_http_client,
            get_pdf_semaphore,
            get_robots_checker,
            get_worker_queue_dependency,
        )
        from downloader.worker_runtime import _init_worker_runtime, _shutdown_worker_runtime

        fake_redis = FakeRedis()
        fake_jm = MagicMock()
        fake_jm.redis_client = fake_redis
        fake_jm.connect = AsyncMock()
        fake_jm.disconnect = AsyncMock()

        fake_sched = MagicMock()
        fake_sched.start = AsyncMock()
        fake_sched.shutdown = AsyncMock()

        registered: list[object] = []
        cfg = Settings()
        cfg.redis.redis_uri = "redis://localhost:6379"
        cfg.worker.metrics_port = 0

        with (
            patch("downloader.worker_runtime.JobManager", return_value=fake_jm),
            patch("downloader.worker_runtime.SchedulerService", return_value=fake_sched),
            patch("downloader.worker_runtime.set_shared_pdf_generator"),
            patch("downloader.worker_runtime.cleanup_pdf_generator", new=AsyncMock()),
            patch("downloader.worker_runtime.shutdown_ocr_executor"),
            patch("downloader.worker_runtime.shutdown_telemetry"),
        ):
            rt = await _init_worker_runtime(
                cfg,
                "init-test-w1",
                on_dead_letter=AsyncMock(),
                register_executor=registered.append,
            )
            assert rt.worker_id == "init-test-w1"
            assert rt.metrics_server is not None
            assert len(registered) == 1
            fake_sched.start.assert_awaited_once_with(paused=False)
            await _shutdown_worker_runtime(rt, set(), logging.getLogger("test"))
            fake_sched.shutdown.assert_awaited_once_with(wait=True)
            fake_jm.disconnect.assert_awaited_once()

        # dependencies.py: remote browser selection, uninitialized errors, and lazy singletons
        api_cfg = Settings()
        api_cfg.worker.deployment_mode = "api"
        api_cfg.worker.remote_browser_enabled = True
        wq = WorkerQueue(fake_redis)
        remote_renderer = build_browser_renderer(api_cfg, work_queue=wq)
        assert isinstance(remote_renderer, RemoteBrowserRenderer)

        empty_req = cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace())))
        with pytest.raises(RuntimeError, match="HTTP client not initialized"):
            await get_http_client(empty_req)
        with pytest.raises(RuntimeError, match="PDF semaphore not initialized"):
            get_pdf_semaphore(empty_req)
        with pytest.raises(RuntimeError, match="Batch semaphore not initialized"):
            get_batch_semaphore(empty_req)

        state = SimpleNamespace(settings=Settings(), job_manager=fake_jm)
        req = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))
        checker = await get_robots_checker(req)
        throttle = await get_domain_throttle(req)
        lazy_wq = await get_worker_queue_dependency(req, fake_jm)
        assert state.robots_checker is checker
        assert state.domain_throttle is throttle
        assert state.worker_queue is lazy_wq
        await checker.close()


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

    def test_otlp_endpoint_appends_v1_traces_path(self) -> None:
        """Base OTLP collector URL gets `/v1/traces` appended while full path is preserved (`R111`)."""
        from downloader.telemetry import _resolve_otlp_traces_endpoint

        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318")
            == "http://otel-collector:4318/v1/traces"
        )
        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318/")
            == "http://otel-collector:4318/v1/traces"
        )
        assert (
            _resolve_otlp_traces_endpoint("http://otel-collector:4318/v1/traces")
            == "http://otel-collector:4318/v1/traces"
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_teardown_isolation_catches_unlisted_exceptions_and_worker_metrics_enforces_auth() -> (
    None
):
    """`S-13` & `S-14`: `_run_teardown_step` isolates arbitrary `Exception` subclasses and worker `/metrics` enforces `DOWNLOADER_KEY`."""
    from downloader.config import AuthConfig
    from downloader.main import _run_teardown_step
    from downloader.worker_runtime import _start_worker_metrics_server

    ran_second_step = False

    async def _bad_step() -> None:
        raise KeyError("unexpected-shutdown-key-error")

    async def _good_step() -> None:
        nonlocal ran_second_step
        ran_second_step = True

    await _run_teardown_step("bad_step", _bad_step)
    await _run_teardown_step("good_step", _good_step)
    assert ran_second_step is True

    auth_cfg = Settings(auth=AuthConfig(api_key="worker-metrics-secret"))
    srv = await _start_worker_metrics_server(0, settings=auth_cfg)
    assert srv.sockets
    port = srv.sockets[0].getsockname()[1]
    try:
        # 1. Unauthenticated scrape -> 401 Unauthorized
        r1, w1 = await asyncio.open_connection("127.0.0.1", port)
        w1.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        await w1.drain()
        unauth_resp = await r1.read(4096)
        w1.close()
        await w1.wait_closed()
        assert unauth_resp.startswith(b"HTTP/1.1 401 Unauthorized")

        # 2. Invalid Bearer token -> 401 Unauthorized
        r2, w2 = await asyncio.open_connection("127.0.0.1", port)
        w2.write(b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer wrong\r\n\r\n")
        await w2.drain()
        wrong_resp = await r2.read(4096)
        w2.close()
        await w2.wait_closed()
        assert wrong_resp.startswith(b"HTTP/1.1 401 Unauthorized")

        # 3. Valid Bearer token -> 200 OK with Prometheus exposition
        r3, w3 = await asyncio.open_connection("127.0.0.1", port)
        w3.write(
            b"GET /metrics HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Authorization: Bearer worker-metrics-secret\r\n\r\n"
        )
        await w3.drain()
        ok_resp = await r3.read(8192)
        w3.close()
        await w3.wait_closed()
        assert ok_resp.startswith(b"HTTP/1.1 200 OK")
        assert b"downloader_uptime_seconds" in ok_resp
    finally:
        srv.close()
        await srv.wait_closed()

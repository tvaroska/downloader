"""Unit tests for split-worker runtime execution, dead-lettering, and CLI lifecycle."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.config import Settings
from downloader.dependencies import (
    get_http_client,
    get_robots_checker,
)
from downloader.job_manager import JobInfo, JobManager, JobStatus
from downloader.ports import BrowserCapacityError
from downloader.worker import process_worker_browser_task
from downloader.worker_queue import (
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerQueue,
)
from tests.fixtures.mock_fixtures import FakeRedis
from tests.unit.test_worker_queue import _encode_resp2_blpop_reply, _read_resp2_command


class TestSplitWorkerRuntime:
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
            get_pdf_semaphore,
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

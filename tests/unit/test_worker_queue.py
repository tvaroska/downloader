"""Unit tests for split-worker Redis/in-memory queue, heartbeats, and reclaim."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.config import Settings
from downloader.dependencies import (
    get_job_manager_dependency,
)
from downloader.job_manager import JobInfo, JobStatus
from downloader.main import app
from downloader.worker import process_worker_batch_envelope, process_worker_browser_task
from downloader.worker_queue import (
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerQueue,
)
from tests.fixtures.mock_fixtures import FakeRedis


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


class TestSplitWorkerQueue:
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

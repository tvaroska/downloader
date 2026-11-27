"""Unit tests for JobManager background lifecycle, health checks, and WATCH/MULTI atomicity."""

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis

from downloader.job_manager import (
    JobInfo,
    JobManager,
    JobStatus,
)
from tests.fixtures.mock_fixtures import FakeRedis, MockPipeline

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client with proper pipeline support."""
    mock_client = AsyncMock()
    mock_client.pipeline = MagicMock(return_value=MockPipeline())
    mock_client.ping = AsyncMock()
    mock_client.close = AsyncMock()
    return mock_client


@pytest.fixture
def mock_connection_pool():
    """Mock Redis connection pool."""
    pool = AsyncMock()
    pool.max_connections = 20
    pool.disconnect = AsyncMock()
    pool.__class__.__name__ = "ConnectionPool"
    return pool


@pytest.fixture
async def job_manager(mock_redis_client):
    """Fixture to create a JobManager instance with a mocked Redis client."""
    manager = JobManager(redis_url="redis://localhost:6379")
    manager.redis_client = mock_redis_client
    yield manager
    # No need for disconnect logic with a mock


@pytest.fixture
async def job_manager_with_pool(mock_redis_client, mock_connection_pool):
    """JobManager with both redis client and connection pool mocked."""
    manager = JobManager(redis_url="redis://localhost:6379")
    manager.redis_client = mock_redis_client
    manager.connection_pool = mock_connection_pool
    yield manager


def _make_kv_job_manager() -> tuple[JobManager, Any, AsyncMock]:
    """Create a JobManager backed by transactional `FakeRedis(decode_responses=True)`."""
    fake_redis: Any = FakeRedis(decode_responses=True)
    fake_redis.close = AsyncMock()

    pool = AsyncMock()
    pool.disconnect = AsyncMock()

    manager = JobManager(redis_url="redis://localhost:6379")
    manager.redis_client = fake_redis
    manager.connection_pool = pool
    return manager, fake_redis, pool


class TestJobManagerBackgroundLifecycle:
    """Tests for full background job lifecycle."""

    @pytest.mark.asyncio
    async def test_background_job_success_lifecycle(self, job_manager, mock_redis_client):
        """Test full job lifecycle: PENDING → RUNNING → COMPLETED."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track status updates
        status_updates = []

        async def capture_status(*args, **kwargs):
            status_updates.append(args[1] if len(args) > 1 else kwargs.get("status"))
            return True

        job_manager.update_job_status = AsyncMock(side_effect=capture_status)
        job_manager.complete_job = AsyncMock(return_value=True)

        async def processor(job_id):
            return [{"result": "success"}], {"total": 1}

        task = await job_manager.start_background_job(job_id, processor)
        await task  # Wait for completion

        # Verify lifecycle
        assert JobStatus.RUNNING in status_updates
        job_manager.complete_job.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_background_job_failure_sets_failed_status(self, job_manager, mock_redis_client):
        """Test exception in processor sets FAILED status."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        status_updates = []
        error_messages = []

        async def capture_status(job_id, status, **kwargs):
            status_updates.append(status)
            if "error_message" in kwargs:
                error_messages.append(kwargs["error_message"])
            return True

        job_manager.update_job_status = AsyncMock(side_effect=capture_status)

        async def failing_processor(job_id):
            raise ValueError("Processing error")

        task = await job_manager.start_background_job(job_id, failing_processor)
        await task  # Wait for completion

        assert JobStatus.FAILED in status_updates
        assert any("Processing error" in msg for msg in error_messages)

    @pytest.mark.asyncio
    async def test_background_job_cancelled_status(self, job_manager, mock_redis_client):
        """Test CancelledError sets CANCELLED status."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        status_updates = []

        async def capture_status(job_id, status, **kwargs):
            status_updates.append(status)
            return True

        job_manager.update_job_status = AsyncMock(side_effect=capture_status)

        async def slow_processor(job_id):
            await asyncio.sleep(10)
            return [], {}

        task = await job_manager.start_background_job(job_id, slow_processor)
        await asyncio.sleep(0.01)  # Let it start
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert JobStatus.CANCELLED in status_updates

    @pytest.mark.asyncio
    async def test_background_job_cleans_up_task_reference(self, job_manager, mock_redis_client):
        """Test task is removed from _background_tasks after completion."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        job_manager.update_job_status = AsyncMock(return_value=True)
        job_manager.complete_job = AsyncMock(return_value=True)

        async def processor(job_id):
            return [], {}

        task = await job_manager.start_background_job(job_id, processor)
        assert job_id in job_manager._background_tasks

        await task

        # Task should be cleaned up after completion
        assert job_id not in job_manager._background_tasks

    @pytest.mark.asyncio
    async def test_result_blob_reports_completed_and_duration(self):
        """Stored JobResult must report status=completed and non-zero total_duration (R9)."""
        manager, _, _ = _make_kv_job_manager()

        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})

        async def processor(_jid: str):
            await asyncio.sleep(0.02)
            return [{"url": "https://example.com", "success": True}], {
                "total_requests": 1,
                "total_duration": 0.42,
            }

        task = await manager.start_background_job(job_id, processor)
        await task

        result = await manager.get_job_results(job_id)
        assert result is not None
        assert result.status == JobStatus.COMPLETED
        assert result.total_duration == pytest.approx(0.42)
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_progress_update_does_not_resurrect_cancelled_job(self):
        """Non-terminal or later status updates cannot overwrite a terminal CANCELLED state (R10)."""
        manager, _, _ = _make_kv_job_manager()

        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        await manager.update_job_status(job_id, JobStatus.RUNNING, progress=10)
        await manager.update_job_status(job_id, JobStatus.CANCELLED)
        await manager.update_job_status(job_id, JobStatus.RUNNING, progress=50, processed_urls=1)
        await manager.update_job_status(job_id, JobStatus.COMPLETED, progress=100)

        info = await manager.get_job_info(job_id)
        assert info is not None
        assert info.status == JobStatus.CANCELLED
        assert info.progress == 10

    @pytest.mark.asyncio
    async def test_cancel_during_webhook_keeps_completed(self):
        """Cancelling while the completion webhook is in-flight keeps status COMPLETED (R10)."""
        manager, _, _ = _make_kv_job_manager()

        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        req = MagicMock()
        req.webhook_url = "https://hooks.example.com/batch"
        webhook_started = asyncio.Event()
        release_webhook = asyncio.Event()

        async def _slow_webhook(*_args, **_kwargs):
            webhook_started.set()
            await release_webhook.wait()

        async def _processor(_jid: str, _req):
            return [{"url": "https://example.com", "success": True}], {"total_duration": 0.1}

        with patch(
            "downloader.services.batch_processor.notify_batch_webhook",
            new=AsyncMock(side_effect=_slow_webhook),
        ):
            task = await manager.start_background_job(job_id, _processor, req)
            await webhook_started.wait()
            cancelled = await manager.cancel_job(job_id)
            assert cancelled is False
            release_webhook.set()
            await task

        info = await manager.get_job_info(job_id)
        assert info is not None
        assert info.status == JobStatus.COMPLETED
        assert info.results_available is True

    @pytest.mark.asyncio
    async def test_cancelled_job_sends_no_completed_webhook_or_result_blob(self):
        """A job cancelled in-flight must store no completed blob and send no completed webhook (`R84`)."""
        manager, _, _ = _make_kv_job_manager()
        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        req = MagicMock()
        req.webhook_url = "https://hooks.example.com/batch"

        async def _processor(jid: str, _req):
            # Simulate concurrent cancel landing in Redis before processor returns.
            await manager.update_job_status(jid, JobStatus.CANCELLED)
            return [{"url": "https://example.com", "success": True}], {
                "total_requests": 1,
                "successful_requests": 1,
                "failed_requests": 0,
                "total_duration": 0.1,
            }

        with patch(
            "downloader.services.batch_processor.notify_batch_webhook",
            new=AsyncMock(),
        ) as mock_hook:
            task = await manager.start_background_job(job_id, _processor, req)
            await task

        info = await manager.get_job_info(job_id)
        assert info is not None
        assert info.status == JobStatus.CANCELLED
        assert info.results_available is False
        assert await manager.get_job_results(job_id) is None
        mock_hook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_status_keeps_results_available_false(self):
        """Terminal FAILED status sets `results_available = False` (`R84`)."""
        manager, _, _ = _make_kv_job_manager()
        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        assert await manager.update_job_status(job_id, JobStatus.RUNNING) is True
        assert (
            await manager.update_job_status(job_id, JobStatus.FAILED, error_message="boom") is True
        )
        info = await manager.get_job_info(job_id)
        assert info is not None
        assert info.status == JobStatus.FAILED
        assert info.results_available is False

    @pytest.mark.asyncio
    async def test_complete_job_payload_cap_updates_summary_and_counters(self):
        """Oversized batch result truncation updates `summary` and `JobInfo` counts (`R113`)."""
        manager, _, _ = _make_kv_job_manager()
        job_id = await manager.create_job(
            {"urls": [{"url": "https://example.com/1"}, {"url": "https://example.com/2"}]}
        )
        await manager.update_job_status(job_id, JobStatus.RUNNING)
        results = [
            {"url": "https://example.com/1", "success": True, "content": "A" * 4000},
            {"url": "https://example.com/2", "success": True, "content": "B" * 4000},
        ]
        summary = {
            "total_requests": 2,
            "successful_requests": 2,
            "failed_requests": 0,
            "success_rate": 100.0,
        }
        applied = await manager.complete_job(job_id, results, summary, max_result_bytes=1200)
        assert applied is True
        stored = await manager.get_job_results(job_id)
        info = await manager.get_job_info(job_id)
        assert stored is not None and info is not None
        assert stored.summary["failed_requests"] >= 1
        assert stored.summary["successful_requests"] + stored.summary["failed_requests"] == 2
        assert info.successful_urls == stored.summary["successful_requests"]
        assert info.failed_urls == stored.summary["failed_requests"]
        assert any(r.get("error_type") == "content_too_large" for r in stored.results)

    @pytest.mark.asyncio
    async def test_wait_for_active_jobs_tracks_in_flight_webhook(self):
        """`wait_for_active_jobs` awaits in-flight webhook delivery tasks (`R100`)."""
        manager, _, _ = _make_kv_job_manager()
        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        req = MagicMock()
        req.webhook_url = "https://hooks.example.com/batch"
        webhook_started = asyncio.Event()
        release_webhook = asyncio.Event()

        async def _slow_webhook(*_args, **_kwargs):
            webhook_started.set()
            await release_webhook.wait()

        async def _processor(_jid: str, _req):
            return [{"url": "https://example.com", "success": True}], {"total_duration": 0.1}

        with patch(
            "downloader.services.batch_processor.notify_batch_webhook",
            new=AsyncMock(side_effect=_slow_webhook),
        ):
            task = await manager.start_background_job(job_id, _processor, req)
            await webhook_started.wait()
            assert await manager.wait_for_active_jobs(0.01) is False
            release_webhook.set()
            assert await manager.wait_for_active_jobs(1.0) is True
            await task


# =============================================================================
# Phase 5: Health Check and Monitoring Tests
# =============================================================================


class TestJobManagerHealthMonitoring:
    """Tests for health check and connection stats."""

    @pytest.mark.asyncio
    async def test_check_redis_health_success(self, job_manager_with_pool, mock_redis_client):
        """Test health check returns True on successful ping."""
        job_manager_with_pool._last_health_check = 0  # Force health check

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1000.0

            result = await job_manager_with_pool._check_redis_health()

            assert result is True
            mock_redis_client.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_redis_health_skips_if_recent(
        self, job_manager_with_pool, mock_redis_client
    ):
        """Test health check is throttled to 60s interval."""
        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1000.0
            job_manager_with_pool._last_health_check = 990.0  # 10 seconds ago

            result = await job_manager_with_pool._check_redis_health()

            assert result is True
            mock_redis_client.ping.assert_not_called()  # Skipped

    @pytest.mark.asyncio
    async def test_check_redis_health_no_client(self):
        """Test health check returns False when client is None."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = None
        manager._last_health_check = 0

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1000.0

            result = await manager._check_redis_health()

            assert result is False

    @pytest.mark.asyncio
    async def test_check_redis_health_returns_false_on_exception(
        self, job_manager_with_pool, mock_redis_client
    ):
        """Test health check returns False on ping failure."""
        job_manager_with_pool._last_health_check = 0
        mock_redis_client.ping.side_effect = redis.ConnectionError("Connection lost")

        with patch("asyncio.get_running_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1000.0

            result = await job_manager_with_pool._check_redis_health()

            assert result is False

    @pytest.mark.asyncio
    async def test_get_connection_stats_disconnected(self):
        """Test get_connection_stats when pool is None."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.connection_pool = None

        stats = await manager.get_connection_stats()

        assert stats == {"status": "disconnected"}

    @pytest.mark.asyncio
    async def test_get_connection_stats_healthy(self, job_manager_with_pool):
        """Test get_connection_stats returns proper stats dict."""
        job_manager_with_pool._check_redis_health = AsyncMock(return_value=True)

        stats = await job_manager_with_pool.get_connection_stats()

        assert stats["status"] == "healthy"
        assert stats["pool_size"] == 20
        assert "health_check_interval" in stats


class TestJobManagerWatchMultiAtomicity:
    """Pin `WATCH`, `MULTI` write buffering, and in-transaction status re-checks (`T-4`)."""

    @pytest.mark.asyncio
    async def test_fake_pipeline_buffers_until_execute_and_requires_multi_after_watch(self) -> None:
        fake = FakeRedis(decode_responses=True)
        await fake.setex("job:1", 60, "v1")

        # Queuing writes after WATCH without MULTI raises immediately
        async with fake.pipeline(transaction=True) as bad_pipe:
            await bad_pipe.watch("job:1")
            with pytest.raises(redis.RedisError, match="multi"):
                bad_pipe.setex("job:1", 60, "v2")

        # Buffered MULTI writes do not mutate store until execute()
        async with fake.pipeline(transaction=True) as buf_pipe:
            await buf_pipe.watch("job:1")
            buf_pipe.multi()
            buf_pipe.setex("job:1", 60, "v2")
            assert await fake.get("job:1") == "v1"
            await buf_pipe.execute()
        assert await fake.get("job:1") == "v2"

        # Concurrent modification after WATCH raises WatchError and discards buffered writes
        async with fake.pipeline(transaction=True) as conflict_pipe:
            await conflict_pipe.watch("job:1")
            await fake.setex("job:1", 60, "external")
            conflict_pipe.multi()
            conflict_pipe.setex("job:1", 60, "stale")
            conflict_pipe.setex("job_result:1", 60, "stale-res")
            with pytest.raises(redis.WatchError):
                await conflict_pipe.execute()
        assert await fake.get("job:1") == "external"
        assert await fake.get("job_result:1") is None

    @pytest.mark.asyncio
    async def test_cancel_during_payload_capping_blocks_completion_via_intransaction_recheck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancel landing after initial pre-check (before WATCH) is caught by `:384` re-check (`T-4`)."""
        import downloader.job_manager as jm_mod

        manager, _, _ = _make_kv_job_manager()
        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        await manager.update_job_status(job_id, JobStatus.RUNNING)

        orig_cap = jm_mod._cap_job_result_payload_with_counts
        cancel_applied = False

        async def _cancel_during_to_thread(func, *args, **kwargs):
            nonlocal cancel_applied
            out = func(*args, **kwargs)
            if not cancel_applied:
                cancel_applied = True
                assert await manager.update_job_status(job_id, JobStatus.CANCELLED) is True
            return out

        monkeypatch.setattr("downloader.job_manager.asyncio.to_thread", _cancel_during_to_thread)
        monkeypatch.setattr(jm_mod, "_cap_job_result_payload_with_counts", orig_cap)

        completed = await manager.complete_job(
            job_id,
            [{"url": "https://example.com", "success": True}],
            {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
        )
        assert completed is False
        info = await manager.get_job_info(job_id)
        assert info is not None and info.status == JobStatus.CANCELLED
        assert await manager.get_job_results(job_id) is None

    @pytest.mark.asyncio
    async def test_cancel_between_watch_read_and_exec_triggers_watcherror_and_aborts(self) -> None:
        """Concurrent cancel between `WATCH`+`GET` and `EXEC` triggers `WatchError` in both update and complete (`T-4`)."""
        manager, fake_redis, _ = _make_kv_job_manager()

        # 1) complete_job: cancel injected right after watched GET on attempt 1
        job_id = await manager.create_job({"urls": [{"url": "https://example.com"}]})
        await manager.update_job_status(job_id, JobStatus.RUNNING)

        orig_get = fake_redis.get
        injected_complete = False
        get_calls = 0

        async def _hooked_get(key: str):
            nonlocal get_calls, injected_complete
            val = await orig_get(key)
            if key == f"job:{job_id}":
                get_calls += 1
                # Call 1 = complete_job initial_info; Call 2 = inside WATCH before MULTI/EXEC
                if get_calls == 2 and not injected_complete:
                    injected_complete = True
                    cancelled_info = JobInfo.model_validate_json(val)
                    cancelled_info.status = JobStatus.CANCELLED
                    await fake_redis.setex(key, 3600, cancelled_info.model_dump_json())
            return val

        fake_redis.get = _hooked_get
        try:
            completed = await manager.complete_job(
                job_id,
                [{"url": "https://example.com", "success": True}],
                {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
            )
        finally:
            fake_redis.get = orig_get

        assert injected_complete is True
        assert get_calls >= 3  # Retried after WatchError!
        assert completed is False
        info = await manager.get_job_info(job_id)
        assert info is not None and info.status == JobStatus.CANCELLED
        assert await manager.get_job_results(job_id) is None

        # 2) update_job_status: cancel injected between WATCH+GET and MULTI/EXEC prevents RUNNING overwrite
        job2 = await manager.create_job({"urls": [{"url": "https://example.com/2"}]})
        await manager.update_job_status(job2, JobStatus.RUNNING, progress=10)
        upd_calls = 0

        async def _hooked_update_get(key: str):
            nonlocal upd_calls
            val = await orig_get(key)
            if key == f"job:{job2}":
                upd_calls += 1
                if upd_calls == 1:
                    cancelled_info = JobInfo.model_validate_json(val)
                    cancelled_info.status = JobStatus.CANCELLED
                    await fake_redis.setex(key, 3600, cancelled_info.model_dump_json())
            return val

        fake_redis.get = _hooked_update_get
        try:
            applied = await manager.update_job_status(job2, JobStatus.RUNNING, progress=80)
        finally:
            fake_redis.get = orig_get

        assert upd_calls == 2
        assert applied is False
        info2 = await manager.get_job_info(job2)
        assert info2 is not None and info2.status == JobStatus.CANCELLED
        assert info2.progress == 10

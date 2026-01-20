"""Tests for the JobManager class."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis

from src.downloader.job_manager import (
    JobInfo,
    JobManager,
    JobResult,
    JobStatus,
    cleanup_job_manager,
    get_job_manager,
)

# =============================================================================
# Mock Classes
# =============================================================================


class MockPipeline:
    """Mock Redis pipeline for transaction testing."""

    def __init__(self):
        self.watch = AsyncMock()
        self.multi = MagicMock()  # multi() is not async
        self.execute = AsyncMock(return_value=[True, True])
        self.setex = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass


class MockPipelineWithWatchError(MockPipeline):
    """Pipeline that raises WatchError for retry testing."""

    def __init__(self, fail_count=1):
        super().__init__()
        self.fail_count = fail_count
        self.attempt = 0

    async def execute(self):
        self.attempt += 1
        if self.attempt <= self.fail_count:
            raise redis.WatchError("Concurrent modification")
        return [True, True]


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


class TestJobManager:
    @pytest.mark.asyncio
    async def test_create_job(self, job_manager, mock_redis_client):
        """Test job creation."""
        request_data = {"urls": ["https://example.com"]}
        job_id = await job_manager.create_job(request_data)

        assert isinstance(job_id, str)
        mock_redis_client.setex.assert_called_once()
        args, _ = mock_redis_client.setex.call_args
        assert args[0] == f"job:{job_id}"
        assert isinstance(args[1], int)
        job_info = JobInfo.model_validate_json(args[2])
        assert job_info.job_id == job_id
        assert job_info.status == JobStatus.PENDING

    @pytest.mark.asyncio
    async def test_get_job_info_found(self, job_manager, mock_redis_client):
        """Test retrieving existing job info."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        retrieved_info = await job_manager.get_job_info(job_id)

        mock_redis_client.get.assert_called_once_with(f"job:{job_id}")
        assert retrieved_info is not None
        assert retrieved_info.job_id == job_id

    @pytest.mark.asyncio
    async def test_get_job_info_not_found(self, job_manager, mock_redis_client):
        """Test retrieving non-existent job info."""
        mock_redis_client.get.return_value = None
        retrieved_info = await job_manager.get_job_info("non_existent_id")
        assert retrieved_info is None

    @pytest.mark.asyncio
    async def test_update_job_status(self, job_manager, mock_redis_client):
        """Test updating job status."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        await job_manager.update_job_status(job_id, JobStatus.COMPLETED, progress=100)

        # The update_job_status now uses pipeline transactions
        # Verify pipeline was used
        mock_redis_client.pipeline.assert_called_once_with(transaction=True)
        # Get the pipeline mock to check its calls
        pipeline = mock_redis_client.pipeline.return_value
        pipeline.setex.assert_called_once()
        args, _ = pipeline.setex.call_args
        updated_info = JobInfo.model_validate_json(args[2])
        assert updated_info.status == JobStatus.COMPLETED
        assert updated_info.progress == 100
        assert updated_info.completed_at is not None

    @pytest.mark.asyncio
    @patch("asyncio.create_task")
    async def test_start_background_job(self, mock_create_task, job_manager):
        """Test starting a background job."""
        job_id = "test_job_id"
        job_processor = AsyncMock(return_value=([], {}))
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        job_manager.redis_client.get.return_value = job_info.model_dump_json()

        await job_manager.start_background_job(job_id, job_processor)

        mock_create_task.assert_called_once()
        assert job_id in job_manager._background_tasks

    @pytest.mark.asyncio
    async def test_cancel_job(self, job_manager):
        """Test cancelling a running job."""
        job_id = "test_job_id"
        cancel_event = asyncio.Event()

        async def dummy_task():
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=10)
            except asyncio.CancelledError:
                raise

        # Use a real task that can be cancelled
        task = asyncio.create_task(dummy_task())
        job_manager._background_tasks[job_id] = task

        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        job_manager.redis_client.get.return_value = job_info.model_dump_json()

        result = await job_manager.cancel_job(job_id)

        assert result is True
        assert task.cancelled()


# =============================================================================
# Phase 1: Connection Lifecycle Tests
# =============================================================================


class TestJobManagerConnection:
    """Tests for connect/disconnect lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_success(self):
        """Test successful Redis connection with pool creation."""
        manager = JobManager(redis_url="redis://localhost:6379")

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_pool_factory:
            mock_pool = AsyncMock()
            mock_pool.max_connections = 20
            mock_pool_factory.return_value = mock_pool

            with patch("redis.asyncio.Redis") as mock_redis_class:
                mock_client = AsyncMock()
                mock_client.ping = AsyncMock()
                mock_redis_class.return_value = mock_client

                await manager.connect()

                # Verify pool created with correct params
                mock_pool_factory.assert_called_once()
                call_kwargs = mock_pool_factory.call_args[1]
                assert call_kwargs["max_connections"] == 20
                assert call_kwargs["decode_responses"] is True

                # Verify Redis client created with pool
                mock_redis_class.assert_called_once_with(connection_pool=mock_pool)

                # Verify ping was called
                mock_client.ping.assert_called_once()

                assert manager.redis_client is not None
                assert manager.connection_pool is not None

    @pytest.mark.asyncio
    async def test_connect_failure_raises_exception(self):
        """Test connection failure propagates exception."""
        manager = JobManager(redis_url="redis://localhost:6379")

        with patch("redis.asyncio.ConnectionPool.from_url") as mock_pool_factory:
            mock_pool_factory.side_effect = redis.ConnectionError("Connection refused")

            with pytest.raises(redis.ConnectionError, match="Connection refused"):
                await manager.connect()

    @pytest.mark.asyncio
    async def test_disconnect_cancels_running_tasks(self, mock_redis_client, mock_connection_pool):
        """Test disconnect cancels all running background tasks."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = mock_redis_client
        manager.connection_pool = mock_connection_pool

        # Create a running task
        cancel_event = asyncio.Event()

        async def dummy_task():
            try:
                await cancel_event.wait()
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(dummy_task())
        manager._background_tasks["job1"] = task

        await manager.disconnect()

        assert task.cancelled()
        assert len(manager._background_tasks) == 0
        mock_redis_client.close.assert_called_once()
        mock_connection_pool.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up_pool(self, mock_redis_client, mock_connection_pool):
        """Test disconnect properly closes Redis client and pool."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = mock_redis_client
        manager.connection_pool = mock_connection_pool

        await manager.disconnect()

        mock_redis_client.close.assert_called_once()
        mock_connection_pool.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_handles_already_done_tasks(
        self, mock_redis_client, mock_connection_pool
    ):
        """Test disconnect handles tasks that are already completed."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = mock_redis_client
        manager.connection_pool = mock_connection_pool

        # Create an already-done task
        async def instant_task():
            return "done"

        task = asyncio.create_task(instant_task())
        await asyncio.sleep(0)  # Let it complete
        manager._background_tasks["job1"] = task

        # Should not raise
        await manager.disconnect()

        assert len(manager._background_tasks) == 0


# =============================================================================
# Phase 2: Job Results Storage and Retrieval Tests
# =============================================================================


class TestJobManagerResults:
    """Tests for store_job_results and get_job_results."""

    @pytest.mark.asyncio
    async def test_store_job_results_success(self, job_manager, mock_redis_client):
        """Test successful job results storage with duration calculation."""
        job_id = "test_job_id"
        started = datetime.now(timezone.utc) - timedelta(seconds=10)
        completed = datetime.now(timezone.utc)

        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            created_at=started - timedelta(seconds=5),
            started_at=started,
            completed_at=completed,
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        results = [{"url": "https://example.com", "status": "success"}]
        summary = {"total": 1, "successful": 1}

        await job_manager.store_job_results(job_id, results, summary)

        mock_redis_client.setex.assert_called_once()
        args, _ = mock_redis_client.setex.call_args
        assert args[0] == f"job_result:{job_id}"

        # Verify stored result
        stored_result = JobResult.model_validate_json(args[2])
        assert stored_result.job_id == job_id
        assert stored_result.results == results
        assert stored_result.summary == summary
        assert stored_result.total_duration > 0  # Duration calculated

    @pytest.mark.asyncio
    async def test_store_job_results_nonexistent_job(self, job_manager, mock_redis_client):
        """Test store_job_results returns early for non-existent job."""
        mock_redis_client.get.return_value = None

        await job_manager.store_job_results("nonexistent", [], {})

        # Should not call setex since job doesn't exist
        mock_redis_client.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_store_job_results_missing_timestamps(self, job_manager, mock_redis_client):
        """Test duration is 0 when timestamps are missing."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=None,  # No started_at
            completed_at=None,  # No completed_at
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        await job_manager.store_job_results(job_id, [], {})

        args, _ = mock_redis_client.setex.call_args
        stored_result = JobResult.model_validate_json(args[2])
        assert stored_result.total_duration == 0.0

    @pytest.mark.asyncio
    async def test_get_job_results_success(self, job_manager, mock_redis_client):
        """Test successful job results retrieval."""
        job_id = "test_job_id"
        now = datetime.now(timezone.utc)
        job_result = JobResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            total_duration=10.5,
            results=[{"url": "https://example.com"}],
            summary={"total": 1},
            created_at=now,
            completed_at=now,
        )
        mock_redis_client.get.return_value = job_result.model_dump_json()

        result = await job_manager.get_job_results(job_id)

        mock_redis_client.get.assert_called_with(f"job_result:{job_id}")
        assert result is not None
        assert result.job_id == job_id
        assert result.total_duration == 10.5

    @pytest.mark.asyncio
    async def test_get_job_results_not_found(self, job_manager, mock_redis_client):
        """Test get_job_results returns None when not found."""
        mock_redis_client.get.return_value = None

        result = await job_manager.get_job_results("nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_job_results_invalid_json(self, job_manager, mock_redis_client):
        """Test get_job_results handles parse errors gracefully."""
        mock_redis_client.get.return_value = "invalid json {"

        result = await job_manager.get_job_results("test_job_id")

        assert result is None


# =============================================================================
# Phase 3: Update Job Status Retry Logic Tests
# =============================================================================


class TestJobManagerRetryLogic:
    """Tests for update_job_status retry behavior."""

    @pytest.mark.asyncio
    async def test_update_job_status_retry_on_watch_error(self, mock_redis_client):
        """Test retry succeeds after WatchError."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = mock_redis_client

        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track attempts across multiple pipeline creations
        attempt_counter = {"count": 0}

        class RetryPipeline:
            def __init__(self):
                self.watch = AsyncMock()
                self.multi = MagicMock()
                self.setex = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self):
                attempt_counter["count"] += 1
                if attempt_counter["count"] <= 1:  # Fail first attempt
                    raise redis.WatchError("Concurrent modification")
                return [True, True]

        mock_redis_client.pipeline = MagicMock(side_effect=lambda **kwargs: RetryPipeline())

        await manager.update_job_status(job_id, JobStatus.RUNNING)

        # Should have attempted twice
        assert attempt_counter["count"] == 2

    @pytest.mark.asyncio
    async def test_update_job_status_exhausts_retries(self, mock_redis_client):
        """Test raises after max retries exceeded."""
        manager = JobManager(redis_url="redis://localhost:6379")
        manager.redis_client = mock_redis_client

        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track attempts
        attempt_counter = {"count": 0}

        class AlwaysFailPipeline:
            def __init__(self):
                self.watch = AsyncMock()
                self.multi = MagicMock()
                self.setex = AsyncMock()

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def execute(self):
                attempt_counter["count"] += 1
                raise redis.WatchError("Concurrent modification")

        mock_redis_client.pipeline = MagicMock(side_effect=lambda **kwargs: AlwaysFailPipeline())

        with pytest.raises(redis.WatchError):
            await manager.update_job_status(job_id, JobStatus.RUNNING, max_retries=3)

        assert attempt_counter["count"] == 3

    @pytest.mark.asyncio
    async def test_update_job_status_sets_started_at_on_running(
        self, job_manager, mock_redis_client
    ):
        """Test RUNNING status sets started_at timestamp."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,  # Not yet started
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        await job_manager.update_job_status(job_id, JobStatus.RUNNING)

        pipeline = mock_redis_client.pipeline.return_value
        args, _ = pipeline.setex.call_args
        updated_info = JobInfo.model_validate_json(args[2])
        assert updated_info.started_at is not None
        assert updated_info.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_update_job_status_nonexistent_job(self, job_manager, mock_redis_client):
        """Test update returns early for non-existent job."""
        mock_redis_client.get.return_value = None

        # Should not raise, just return early
        await job_manager.update_job_status("nonexistent", JobStatus.RUNNING)

        # Pipeline should still be called but setex should not be
        pipeline = mock_redis_client.pipeline.return_value
        pipeline.setex.assert_not_called()


# =============================================================================
# Phase 4: Background Job Lifecycle Tests
# =============================================================================


class TestJobManagerBackgroundLifecycle:
    """Tests for full background job lifecycle."""

    @pytest.mark.asyncio
    async def test_background_job_success_lifecycle(self, job_manager, mock_redis_client):
        """Test full job lifecycle: PENDING → RUNNING → COMPLETED."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track status updates
        status_updates = []

        async def capture_status(*args, **kwargs):
            status_updates.append(args[1] if len(args) > 1 else kwargs.get("status"))

        job_manager.update_job_status = AsyncMock(side_effect=capture_status)
        job_manager.store_job_results = AsyncMock()

        async def processor(job_id):
            return [{"result": "success"}], {"total": 1}

        task = await job_manager.start_background_job(job_id, processor)
        await task  # Wait for completion

        # Verify lifecycle
        assert JobStatus.RUNNING in status_updates
        assert JobStatus.COMPLETED in status_updates
        job_manager.store_job_results.assert_called_once()

    @pytest.mark.asyncio
    async def test_background_job_failure_sets_failed_status(self, job_manager, mock_redis_client):
        """Test exception in processor sets FAILED status."""
        job_id = "test_job_id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        status_updates = []
        error_messages = []

        async def capture_status(job_id, status, **kwargs):
            status_updates.append(status)
            if "error_message" in kwargs:
                error_messages.append(kwargs["error_message"])

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
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        status_updates = []

        async def capture_status(job_id, status, **kwargs):
            status_updates.append(status)

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
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        job_manager.update_job_status = AsyncMock()
        job_manager.store_job_results = AsyncMock()

        async def processor(job_id):
            return [], {}

        task = await job_manager.start_background_job(job_id, processor)
        assert job_id in job_manager._background_tasks

        await task

        # Task should be cleaned up after completion
        assert job_id not in job_manager._background_tasks


# =============================================================================
# Phase 5: Health Check and Monitoring Tests
# =============================================================================


class TestJobManagerHealthMonitoring:
    """Tests for health check and connection stats."""

    @pytest.mark.asyncio
    async def test_check_redis_health_success(self, job_manager_with_pool, mock_redis_client):
        """Test health check returns True on successful ping."""
        job_manager_with_pool._last_health_check = 0  # Force health check

        with patch("asyncio.get_event_loop") as mock_loop:
            mock_loop.return_value.time.return_value = 1000.0

            result = await job_manager_with_pool._check_redis_health()

            assert result is True
            mock_redis_client.ping.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_redis_health_skips_if_recent(
        self, job_manager_with_pool, mock_redis_client
    ):
        """Test health check is throttled to 60s interval."""
        with patch("asyncio.get_event_loop") as mock_loop:
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

        with patch("asyncio.get_event_loop") as mock_loop:
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

        with patch("asyncio.get_event_loop") as mock_loop:
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

    @pytest.mark.asyncio
    async def test_cleanup_expired_jobs_removes_done_tasks(
        self, job_manager_with_pool, mock_redis_client
    ):
        """Test cleanup_expired_jobs removes completed task references."""
        job_manager_with_pool._check_redis_health = AsyncMock(return_value=True)

        # Create a done task
        async def instant():
            return "done"

        task = asyncio.create_task(instant())
        await asyncio.sleep(0)  # Let it complete
        job_manager_with_pool._background_tasks["done_job"] = task

        # Create a running task
        running_event = asyncio.Event()

        async def wait_task():
            await running_event.wait()

        running_task = asyncio.create_task(wait_task())
        job_manager_with_pool._background_tasks["running_job"] = running_task

        await job_manager_with_pool.cleanup_expired_jobs()

        # Done task should be cleaned up
        assert "done_job" not in job_manager_with_pool._background_tasks
        # Running task should remain
        assert "running_job" in job_manager_with_pool._background_tasks

        # Cleanup
        running_event.set()
        await running_task

    @pytest.mark.asyncio
    async def test_cleanup_expired_jobs_skips_on_health_failure(
        self, job_manager_with_pool, mock_redis_client
    ):
        """Test cleanup is skipped when health check fails."""
        job_manager_with_pool._check_redis_health = AsyncMock(return_value=False)

        # Add a done task
        async def instant():
            return "done"

        task = asyncio.create_task(instant())
        await asyncio.sleep(0)
        job_manager_with_pool._background_tasks["done_job"] = task

        await job_manager_with_pool.cleanup_expired_jobs()

        # Should not be cleaned up due to health check failure
        assert "done_job" in job_manager_with_pool._background_tasks


# =============================================================================
# Global Singleton Tests
# =============================================================================


class TestJobManagerSingleton:
    """Tests for global get_job_manager and cleanup_job_manager."""

    @pytest.mark.asyncio
    async def test_get_job_manager_creates_instance(self):
        """Test get_job_manager creates and connects instance."""
        import src.downloader.job_manager as jm

        original = jm._job_manager
        jm._job_manager = None

        try:
            with patch.object(JobManager, "connect", new_callable=AsyncMock) as mock_connect:
                with patch.dict("os.environ", {"REDIS_URI": "redis://test:6379"}):
                    manager = await get_job_manager()

                    assert manager is not None
                    mock_connect.assert_called_once()
        finally:
            if jm._job_manager:
                jm._job_manager.redis_client = None  # Prevent actual disconnect
            jm._job_manager = original

    @pytest.mark.asyncio
    async def test_get_job_manager_returns_existing(self):
        """Test get_job_manager returns existing instance."""
        import src.downloader.job_manager as jm

        original = jm._job_manager
        mock_manager = MagicMock()
        jm._job_manager = mock_manager

        try:
            result = await get_job_manager()
            assert result is mock_manager
        finally:
            jm._job_manager = original

    @pytest.mark.asyncio
    async def test_cleanup_job_manager_disconnects(self):
        """Test cleanup_job_manager disconnects and clears global."""
        import src.downloader.job_manager as jm

        original = jm._job_manager
        mock_manager = AsyncMock()
        mock_manager.disconnect = AsyncMock()
        jm._job_manager = mock_manager

        try:
            await cleanup_job_manager()

            mock_manager.disconnect.assert_called_once()
            assert jm._job_manager is None
        finally:
            jm._job_manager = original

"""Unit tests for JobManager CRUD, connection pooling, results, and retry logic."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis

from downloader.job_manager import (
    JobInfo,
    JobManager,
    JobResult,
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
            created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
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

        def _fake_create_task(coro):
            coro.close()
            return MagicMock()

        mock_create_task.side_effect = _fake_create_task
        job_id = "test_job_id"
        job_processor = AsyncMock(return_value=([], {}))
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=datetime.now(UTC),
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
            created_at=datetime.now(UTC),
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

    @pytest.mark.asyncio
    async def test_disconnect_with_real_background_job(self):
        """Test disconnect cancels real start_background_job tasks and closes pool."""
        manager, client, pool = _make_kv_job_manager()

        job1 = await manager.create_job({"urls": [{"url": "https://example.com/1"}]})
        job2 = await manager.create_job({"urls": [{"url": "https://example.com/2"}]})
        started = asyncio.Event()
        gate = asyncio.Event()

        async def slow_processor(_job_id: str):
            started.set()
            await gate.wait()
            return [], {}

        t1 = await manager.start_background_job(job1, slow_processor)
        t2 = await manager.start_background_job(job2, slow_processor)
        await started.wait()
        await asyncio.sleep(0.01)

        await manager.disconnect()

        assert t1.done()
        assert t2.done()
        assert len(manager._background_tasks) == 0
        client.close.assert_awaited_once()
        pool.disconnect.assert_awaited_once()
        info1 = await manager.get_job_info(job1)
        info2 = await manager.get_job_info(job2)
        assert info1 is not None and info1.status == JobStatus.CANCELLED
        assert info2 is not None and info2.status == JobStatus.CANCELLED


# =============================================================================
# Phase 2: Job Results Storage and Retrieval Tests
# =============================================================================


class TestJobManagerResults:
    """Tests for store_job_results and get_job_results."""

    @pytest.mark.asyncio
    async def test_store_job_results_success(self, job_manager, mock_redis_client):
        """Test successful job results storage with duration calculation."""
        job_id = "test_job_id"
        started = datetime.now(UTC) - timedelta(seconds=10)
        completed = datetime.now(UTC)

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
            created_at=datetime.now(UTC),
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
        now = datetime.now(UTC)
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
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track attempts across multiple pipeline creations
        attempt_counter = {"count": 0}

        class RetryPipeline:
            def __init__(self):
                self.watch = AsyncMock()
                self.multi = MagicMock()
                self.setex = MagicMock()

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
            created_at=datetime.now(UTC),
            request_data={},
        )
        mock_redis_client.get.return_value = job_info.model_dump_json()

        # Track attempts
        attempt_counter = {"count": 0}

        class AlwaysFailPipeline:
            def __init__(self):
                self.watch = AsyncMock()
                self.multi = MagicMock()
                self.setex = MagicMock()

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
            created_at=datetime.now(UTC),
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

"""Tests for the JobManager class."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.job_manager import JobInfo, JobManager, JobStatus


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client."""
    return AsyncMock()


@pytest.fixture
async def job_manager(mock_redis_client):
    """Fixture to create a JobManager instance with a mocked Redis client."""
    manager = JobManager(redis_url="redis://localhost:6379")
    manager.redis_client = mock_redis_client
    yield manager
    # No need for disconnect logic with a mock


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

        mock_redis_client.setex.assert_called_once()
        args, _ = mock_redis_client.setex.call_args
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

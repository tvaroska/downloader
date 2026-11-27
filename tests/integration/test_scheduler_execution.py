"""Integration tests for scheduled job execution and retry behavior."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from downloader.config import SchedulerConfig
from downloader.models.schedule import ExecutionStatus
from downloader.scheduler.executor import MAX_ATTEMPTS, ScheduledJobExecutor
from downloader.scheduler.service import SchedulerService
from downloader.scheduler.storage import ExecutionStorage


@pytest.mark.unit
class TestJobExecution:
    """Integration tests for ScheduledJobExecutor.execute()."""

    @pytest.fixture
    def mock_http_client(self):
        """Create a mock HTTP client."""
        mock = AsyncMock()
        mock.download.return_value = (
            b"<html><body>Hello World</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 40,
            },
        )
        return mock

    @pytest.fixture
    def mock_storage(self):
        """Create a mock execution storage."""
        return AsyncMock(spec=ExecutionStorage)

    @pytest.fixture
    def executor(self, mock_http_client, mock_storage):
        """Create a ScheduledJobExecutor with mocks."""
        return ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
            pdf_generator=None,
            pdf_semaphore=None,
        )

    @pytest.mark.asyncio
    async def test_execute_success(self, executor, mock_storage):
        """Test successful execution returns COMPLETED status."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        assert result.status == ExecutionStatus.COMPLETED
        assert result.success is True
        assert result.attempt == 1
        mock_storage.store_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_records_duration(self, executor, mock_storage):
        """Test that duration is recorded correctly."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        assert result.duration is not None
        assert result.duration >= 0

    @pytest.mark.asyncio
    async def test_execute_records_content_size(self, executor, mock_storage):
        """Test that content size is recorded for string output."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        assert result.content_size is not None
        assert result.content_size > 0

    @pytest.mark.asyncio
    async def test_execute_stores_in_storage(self, executor, mock_storage):
        """Test that execution is stored via storage.store_execution."""
        await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        mock_storage.store_execution.assert_called_once()
        stored_execution = mock_storage.store_execution.call_args[0][0]
        assert stored_execution.schedule_id == "sched-123"

    @pytest.mark.asyncio
    async def test_execute_validates_url(self, executor, mock_http_client, mock_storage):
        """Test that invalid URL fails execution."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="not-a-valid-url",
            format="text",
            headers=None,
        )

        assert result.success is False
        assert result.status == ExecutionStatus.FAILED
        assert "error" in result.error_message.lower() or "url" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_execute_text_format(self, executor, mock_storage):
        """Test text format conversion."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_markdown_format(self, executor, mock_storage):
        """Test markdown format conversion."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="markdown",
            headers=None,
        )

        assert result.success is True

    @pytest.mark.asyncio
    async def test_execute_html_format(self, executor, mock_storage):
        """Test HTML format returns decoded content."""
        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="html",
            headers=None,
        )

        assert result.success is True
        assert result.content_size is not None

    @pytest.mark.asyncio
    async def test_execute_json_format(self, executor, mock_http_client, mock_storage):
        """Test JSON format returns raw bytes."""
        mock_http_client.download.return_value = (
            b'{"key": "value"}',
            {"content_type": "application/json"},
        )

        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com/api",
            format="json",
            headers=None,
        )

        assert result.success is True


# ============= Retry Logic Tests =============


@pytest.mark.unit
class TestRetryLogic:
    """Integration tests for retry behavior in ScheduledJobExecutor."""

    @pytest.fixture
    def mock_storage(self):
        """Create a mock execution storage."""
        return AsyncMock(spec=ExecutionStorage)

    @pytest.mark.asyncio
    async def test_retry_succeeds_second_attempt(self, mock_storage):
        """Test retry succeeds after first failure."""
        mock_http_client = AsyncMock()

        # Fail first, succeed second
        mock_http_client.download.side_effect = [
            Exception("Connection timeout"),
            (
                b"<html>Success</html>",
                {"content_type": "text/html"},
            ),
        ]

        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await executor.execute(
                schedule_id="sched-123",
                url="https://example.com",
                format="text",
                headers=None,
            )

        assert result.success is True
        assert result.attempt == 2
        # Should have stored 2 execution records
        assert mock_storage.store_execution.call_count == 2

    @pytest.mark.asyncio
    async def test_retry_succeeds_third_attempt(self, mock_storage):
        """Test retry succeeds on third attempt."""
        mock_http_client = AsyncMock()

        # Fail twice, succeed third
        mock_http_client.download.side_effect = [
            Exception("Connection timeout"),
            Exception("Server error"),
            (
                b"<html>Success</html>",
                {"content_type": "text/html"},
            ),
        ]

        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await executor.execute(
                schedule_id="sched-123",
                url="https://example.com",
                format="text",
                headers=None,
            )

        assert result.success is True
        assert result.attempt == 3
        assert mock_storage.store_execution.call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted_all_attempts(self, mock_storage):
        """Test all 3 attempts fail, returns FAILED status."""
        mock_http_client = AsyncMock()

        # All attempts fail
        mock_http_client.download.side_effect = [
            Exception("Error 1"),
            Exception("Error 2"),
            Exception("Error 3"),
        ]

        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await executor.execute(
                schedule_id="sched-123",
                url="https://example.com",
                format="text",
                headers=None,
            )

        assert result.success is False
        assert result.status == ExecutionStatus.FAILED
        assert result.attempt == MAX_ATTEMPTS
        assert mock_storage.store_execution.call_count == MAX_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retry_stores_all_attempts(self, mock_storage):
        """Test each attempt is stored in history."""
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = [
            Exception("Error 1"),
            (
                b"<html>Success</html>",
                {"content_type": "text/html"},
            ),
        ]

        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
        )

        with patch("asyncio.sleep", new_callable=AsyncMock):
            await executor.execute(
                schedule_id="sched-123",
                url="https://example.com",
                format="text",
                headers=None,
            )

        assert mock_storage.store_execution.call_count == 2

        # Verify first attempt was stored as failed
        first_call_execution = mock_storage.store_execution.call_args_list[0][0][0]
        assert first_call_execution.success is False
        assert first_call_execution.attempt == 1

        # Verify second attempt was stored as success
        second_call_execution = mock_storage.store_execution.call_args_list[1][0][0]
        assert second_call_execution.success is True
        assert second_call_execution.attempt == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self, mock_storage):
        """Test no retry when first attempt succeeds."""
        mock_http_client = AsyncMock()
        mock_http_client.download.return_value = (
            b"<html>Success</html>",
            {"content_type": "text/html"},
        )

        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
        )

        result = await executor.execute(
            schedule_id="sched-123",
            url="https://example.com",
            format="text",
            headers=None,
        )

        assert result.success is True
        assert result.attempt == 1
        assert mock_storage.store_execution.call_count == 1


# ============= Schedule Persistence Tests =============


@pytest.mark.unit
class TestSchedulePersistence:
    """Integration tests for schedule persistence via real APScheduler."""

    @pytest.fixture
    def scheduler_service(self):
        """Create a real SchedulerService with MemoryJobStore."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri=None, settings=settings)
        return service

    @pytest.mark.asyncio
    async def test_schedule_persists_in_job_store(self, scheduler_service):
        """Test job is stored in APScheduler job store."""
        await scheduler_service.start()

        try:
            from apscheduler.triggers.cron import CronTrigger

            async def dummy_job():
                pass

            trigger = CronTrigger.from_crontab("0 9 * * *")
            scheduler_service.scheduler.add_job(
                func=dummy_job,
                trigger=trigger,
                id="test-job-1",
                name="Test Job",
            )

            jobs = scheduler_service.get_jobs()
            assert len(jobs) == 1
            assert jobs[0].id == "test-job-1"
        finally:
            await scheduler_service.shutdown()

    @pytest.mark.asyncio
    async def test_schedule_kwargs_preserved(self, scheduler_service):
        """Test URL, format, headers preserved in job kwargs."""
        await scheduler_service.start()

        try:
            from apscheduler.triggers.cron import CronTrigger

            async def dummy_job(**kwargs):
                pass

            trigger = CronTrigger.from_crontab("0 9 * * *")
            job_kwargs = {
                "schedule_id": "sched-123",
                "url": "https://example.com",
                "format": "markdown",
                "headers": {"Authorization": "Bearer token"},
            }
            scheduler_service.scheduler.add_job(
                func=dummy_job,
                trigger=trigger,
                id="test-job-2",
                name="Test Job",
                kwargs=job_kwargs,
            )

            job = scheduler_service.get_job("test-job-2")
            assert job is not None
            assert job.kwargs["url"] == "https://example.com"
            assert job.kwargs["format"] == "markdown"
            assert job.kwargs["headers"]["Authorization"] == "Bearer token"
        finally:
            await scheduler_service.shutdown()

    @pytest.mark.asyncio
    async def test_disabled_schedule_paused(self, scheduler_service):
        """Test disabled schedules have no next_run_time."""
        await scheduler_service.start()

        try:
            from apscheduler.triggers.cron import CronTrigger

            async def dummy_job():
                pass

            trigger = CronTrigger.from_crontab("0 9 * * *")
            job = scheduler_service.scheduler.add_job(
                func=dummy_job,
                trigger=trigger,
                id="test-job-3",
                name="Disabled Job",
            )
            job.pause()

            paused_job = scheduler_service.get_job("test-job-3")
            assert paused_job.next_run_time is None
        finally:
            await scheduler_service.shutdown()

    @pytest.mark.asyncio
    async def test_schedule_next_run_calculated(self, scheduler_service):
        """Test next_run_time is correctly calculated."""
        await scheduler_service.start()

        try:
            from apscheduler.triggers.cron import CronTrigger

            async def dummy_job():
                pass

            trigger = CronTrigger.from_crontab("* * * * *")  # Every minute
            scheduler_service.scheduler.add_job(
                func=dummy_job,
                trigger=trigger,
                id="test-job-4",
                name="Frequent Job",
            )

            job = scheduler_service.get_job("test-job-4")
            assert job.next_run_time is not None
            # Next run should be in the future (within ~1 minute)
            assert job.next_run_time > datetime.now(UTC)
            assert job.next_run_time < datetime.now(UTC) + timedelta(minutes=2)
        finally:
            await scheduler_service.shutdown()

    @pytest.mark.asyncio
    async def test_multiple_schedules_independent(self, scheduler_service):
        """Test multiple schedules don't interfere with each other."""
        await scheduler_service.start()

        try:
            from apscheduler.triggers.cron import CronTrigger

            async def dummy_job():
                pass

            for i in range(3):
                trigger = CronTrigger.from_crontab(f"0 {9 + i} * * *")
                scheduler_service.scheduler.add_job(
                    func=dummy_job,
                    trigger=trigger,
                    id=f"test-job-{i}",
                    name=f"Test Job {i}",
                )

            jobs = scheduler_service.get_jobs()
            assert len(jobs) == 3

            # Remove one job
            scheduler_service.remove_job("test-job-1")

            remaining_jobs = scheduler_service.get_jobs()
            assert len(remaining_jobs) == 2
            assert "test-job-0" in [j.id for j in remaining_jobs]
            assert "test-job-2" in [j.id for j in remaining_jobs]
        finally:
            await scheduler_service.shutdown()

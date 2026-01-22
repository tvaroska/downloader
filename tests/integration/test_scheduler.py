"""Integration tests for scheduler components.

Tests for:
- Schedule CRUD routes
- Job execution with mock HTTP client
- Retry logic
- Schedule persistence
- Execution history
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.config import SchedulerConfig
from src.downloader.dependencies import (
    get_execution_storage_dependency,
    get_scheduler_dependency,
)
from src.downloader.main import app
from src.downloader.models.schedule import ExecutionStatus, ScheduleExecution
from src.downloader.scheduler.executor import MAX_ATTEMPTS, ScheduledJobExecutor
from src.downloader.scheduler.service import SchedulerService
from src.downloader.scheduler.storage import ExecutionStorage

# ============= Route Tests =============


@pytest.mark.integration
class TestScheduleRoutes:
    """Integration tests for schedule CRUD endpoints."""

    @pytest.fixture
    def api_client(self):
        """Create a TestClient for the app."""
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    @pytest.fixture
    def mock_scheduler_svc(self):
        """Create a mock scheduler service."""
        mock_svc = MagicMock(spec=SchedulerService)
        mock_svc.is_running.return_value = True
        mock_svc.get_jobs.return_value = []
        mock_svc.get_job.return_value = None

        # Mock the scheduler property
        mock_scheduler = MagicMock()
        type(mock_svc).scheduler = property(lambda self: mock_scheduler)
        mock_svc._scheduler = mock_scheduler

        return mock_svc

    @pytest.fixture
    def override_scheduler(self, mock_scheduler_svc):
        """Override scheduler dependency."""

        async def mock_get_scheduler():
            return mock_scheduler_svc

        app.dependency_overrides[get_scheduler_dependency] = mock_get_scheduler
        yield mock_scheduler_svc
        app.dependency_overrides.pop(get_scheduler_dependency, None)

    @pytest.fixture
    def mock_storage(self):
        """Create a mock execution storage."""
        mock = AsyncMock(spec=ExecutionStorage)
        mock.get_executions.return_value = []
        mock.get_execution_count.return_value = 0
        return mock

    @pytest.fixture
    def override_storage(self, mock_storage):
        """Override execution storage dependency."""

        async def mock_get_storage():
            return mock_storage

        app.dependency_overrides[get_execution_storage_dependency] = mock_get_storage
        yield mock_storage
        app.dependency_overrides.pop(get_execution_storage_dependency, None)

    def test_create_schedule_success(self, api_client, override_scheduler):
        """Test POST /schedules creates a schedule successfully."""
        mock_job = MagicMock()
        mock_job.id = "test-schedule-id"
        mock_job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
        override_scheduler._scheduler.add_job.return_value = mock_job

        request_data = {
            "name": "Test Schedule",
            "url": "https://example.com/page",
            "cron_expression": "0 9 * * *",
            "format": "text",
        }

        response = api_client.post("/schedules", json=request_data)

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test Schedule"
        assert data["url"] == "https://example.com/page"
        assert data["cron_expression"] == "0 9 * * *"
        assert data["format"] == "text"
        assert data["enabled"] is True

    def test_create_schedule_validates_cron(self, api_client, override_scheduler):
        """Test POST /schedules rejects invalid cron expressions."""
        request_data = {
            "name": "Test Schedule",
            "url": "https://example.com/page",
            "cron_expression": "invalid cron",
            "format": "text",
        }

        response = api_client.post("/schedules", json=request_data)

        assert response.status_code == 422  # Validation error

    def test_create_schedule_disabled(self, api_client, override_scheduler):
        """Test POST /schedules with enabled=False pauses the job."""
        mock_job = MagicMock()
        mock_job.id = "test-schedule-id"
        mock_job.next_run_time = None  # Paused
        mock_job.pause = MagicMock()
        override_scheduler._scheduler.add_job.return_value = mock_job

        request_data = {
            "name": "Disabled Schedule",
            "url": "https://example.com/page",
            "cron_expression": "0 9 * * *",
            "format": "text",
            "enabled": False,
        }

        response = api_client.post("/schedules", json=request_data)

        assert response.status_code == 200
        mock_job.pause.assert_called_once()

    def test_list_schedules_empty(self, api_client, override_scheduler):
        """Test GET /schedules returns empty list initially."""
        override_scheduler.get_jobs.return_value = []

        response = api_client.get("/schedules")

        assert response.status_code == 200
        data = response.json()
        assert data["schedules"] == []
        assert data["total"] == 0

    def test_list_schedules_with_jobs(self, api_client, override_scheduler):
        """Test GET /schedules returns all scheduled jobs."""
        mock_job = MagicMock()
        mock_job.id = "sched-123"
        mock_job.name = "Test Job"
        mock_job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_job.kwargs = {
            "schedule_id": "sched-123",
            "url": "https://example.com",
            "format": "text",
            "headers": None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        mock_job.trigger = MagicMock()
        mock_job.trigger.fields = [MagicMock()] * 7  # APScheduler fields
        for i, val in enumerate(["0", "*", "9", "*", "*", "*", "0"]):
            mock_job.trigger.fields[i].__str__ = lambda self, v=val: v

        override_scheduler.get_jobs.return_value = [mock_job]

        response = api_client.get("/schedules")

        assert response.status_code == 200
        data = response.json()
        assert len(data["schedules"]) == 1
        assert data["total"] == 1
        assert data["schedules"][0]["id"] == "sched-123"
        assert data["schedules"][0]["name"] == "Test Job"

    def test_get_schedule_found(self, api_client, override_scheduler):
        """Test GET /schedules/{id} returns specific job."""
        mock_job = MagicMock()
        mock_job.id = "sched-123"
        mock_job.name = "Test Job"
        mock_job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
        mock_job.kwargs = {
            "schedule_id": "sched-123",
            "url": "https://example.com",
            "format": "markdown",
            "headers": {"Authorization": "Bearer token"},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        mock_job.trigger = MagicMock()
        mock_job.trigger.fields = [MagicMock()] * 7

        override_scheduler.get_job.return_value = mock_job

        response = api_client.get("/schedules/sched-123")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "sched-123"
        assert data["format"] == "markdown"
        assert data["headers"] == {"Authorization": "Bearer token"}

    def test_get_schedule_not_found(self, api_client, override_scheduler):
        """Test GET /schedules/{id} returns 404 for non-existent schedule."""
        override_scheduler.get_job.return_value = None

        response = api_client.get("/schedules/nonexistent")

        assert response.status_code == 404
        data = response.json()
        assert "not found" in data["detail"]["error"].lower()

    def test_delete_schedule_success(self, api_client, override_scheduler):
        """Test DELETE /schedules/{id} removes job."""
        mock_job = MagicMock()
        mock_job.id = "sched-123"
        override_scheduler.get_job.return_value = mock_job
        override_scheduler.remove_job = MagicMock()

        response = api_client.delete("/schedules/sched-123")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        override_scheduler.remove_job.assert_called_once_with("sched-123")

    def test_delete_schedule_not_found(self, api_client, override_scheduler):
        """Test DELETE /schedules/{id} returns 404."""
        override_scheduler.get_job.return_value = None

        response = api_client.delete("/schedules/nonexistent")

        assert response.status_code == 404

    def test_scheduler_unavailable_503(self, api_client):
        """Test endpoints return 503 when scheduler is None."""

        async def mock_get_scheduler():
            return None

        app.dependency_overrides[get_scheduler_dependency] = mock_get_scheduler

        try:
            response = api_client.get("/schedules")
            assert response.status_code == 503
            assert "not available" in response.json()["detail"]["error"].lower()
        finally:
            app.dependency_overrides.pop(get_scheduler_dependency, None)

    def test_schedule_with_custom_headers(self, api_client, override_scheduler):
        """Test creating schedule with custom headers."""
        mock_job = MagicMock()
        mock_job.id = "test-schedule-id"
        mock_job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
        override_scheduler._scheduler.add_job.return_value = mock_job

        request_data = {
            "name": "Auth Schedule",
            "url": "https://api.example.com/data",
            "cron_expression": "*/15 * * * *",
            "format": "json",
            "headers": {"Authorization": "Bearer token123", "X-Custom": "value"},
        }

        response = api_client.post("/schedules", json=request_data)

        assert response.status_code == 200
        data = response.json()
        assert data["headers"] == {"Authorization": "Bearer token123", "X-Custom": "value"}

    def test_schedule_all_formats(self, api_client, override_scheduler):
        """Test that all output formats are accepted."""
        mock_job = MagicMock()
        mock_job.id = "test-schedule-id"
        mock_job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
        override_scheduler._scheduler.add_job.return_value = mock_job

        valid_formats = ["text", "html", "markdown", "pdf", "json", "raw"]

        for fmt in valid_formats:
            request_data = {
                "name": f"Schedule {fmt}",
                "url": "https://example.com/page",
                "cron_expression": "0 9 * * *",
                "format": fmt,
            }

            response = api_client.post("/schedules", json=request_data)
            assert response.status_code == 200, f"Format {fmt} should be accepted"
            assert response.json()["format"] == fmt


# ============= Execution History Tests =============


@pytest.mark.integration
class TestExecutionHistory:
    """Integration tests for execution history endpoint."""

    @pytest.fixture
    def api_client(self):
        """Create a TestClient for the app."""
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client

    @pytest.fixture
    def mock_scheduler_svc(self):
        """Create a mock scheduler service."""
        mock_svc = MagicMock(spec=SchedulerService)
        mock_svc.is_running.return_value = True

        # Create a mock job
        mock_job = MagicMock()
        mock_job.id = "sched-123"
        mock_job.name = "Test Schedule"
        mock_svc.get_job.return_value = mock_job

        return mock_svc

    @pytest.fixture
    def override_scheduler(self, mock_scheduler_svc):
        """Override scheduler dependency."""

        async def mock_get_scheduler():
            return mock_scheduler_svc

        app.dependency_overrides[get_scheduler_dependency] = mock_get_scheduler
        yield mock_scheduler_svc
        app.dependency_overrides.pop(get_scheduler_dependency, None)

    @pytest.fixture
    def mock_storage(self):
        """Create a mock execution storage."""
        return AsyncMock(spec=ExecutionStorage)

    @pytest.fixture
    def override_storage(self, mock_storage):
        """Override execution storage dependency."""

        async def mock_get_storage():
            return mock_storage

        app.dependency_overrides[get_execution_storage_dependency] = mock_get_storage
        yield mock_storage
        app.dependency_overrides.pop(get_execution_storage_dependency, None)

    def test_get_history_empty(self, api_client, override_scheduler, override_storage):
        """Test GET /schedules/{id}/history returns empty initially."""
        override_storage.get_executions.return_value = []
        override_storage.get_execution_count.return_value = 0

        response = api_client.get("/schedules/sched-123/history")

        assert response.status_code == 200
        data = response.json()
        assert data["executions"] == []
        assert data["total"] == 0

    def test_get_history_with_executions(self, api_client, override_scheduler, override_storage):
        """Test GET /schedules/{id}/history returns executions."""
        execution = ScheduleExecution(
            execution_id="exec-1",
            schedule_id="sched-123",
            status=ExecutionStatus.COMPLETED,
            started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            completed_at=datetime.now(timezone.utc) - timedelta(minutes=4),
            duration=60.0,
            success=True,
            content_size=1024,
            error_message=None,
            attempt=1,
        )
        override_storage.get_executions.return_value = [execution]
        override_storage.get_execution_count.return_value = 1

        response = api_client.get("/schedules/sched-123/history")

        assert response.status_code == 200
        data = response.json()
        assert len(data["executions"]) == 1
        assert data["executions"][0]["execution_id"] == "exec-1"
        assert data["executions"][0]["success"] is True

    def test_get_history_pagination(self, api_client, override_scheduler, override_storage):
        """Test pagination with limit and offset."""
        override_storage.get_executions.return_value = []
        override_storage.get_execution_count.return_value = 50

        response = api_client.get("/schedules/sched-123/history?limit=10&offset=20")

        assert response.status_code == 200
        data = response.json()
        assert data["limit"] == 10
        assert data["offset"] == 20
        assert data["total"] == 50

        # Verify storage was called with correct pagination
        override_storage.get_executions.assert_called_once_with("sched-123", 10, 20)

    def test_get_history_schedule_not_found(self, api_client, override_scheduler, override_storage):
        """Test returns 404 for non-existent schedule."""
        override_scheduler.get_job.return_value = None

        response = api_client.get("/schedules/nonexistent/history")

        assert response.status_code == 404

    def test_get_history_storage_unavailable(self, api_client, override_scheduler):
        """Test returns 503 when storage is None."""

        async def mock_get_storage():
            return None

        app.dependency_overrides[get_execution_storage_dependency] = mock_get_storage

        try:
            response = api_client.get("/schedules/sched-123/history")
            assert response.status_code == 503
        finally:
            app.dependency_overrides.pop(get_execution_storage_dependency, None)


# ============= Job Execution Tests =============


@pytest.mark.integration
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


@pytest.mark.integration
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


@pytest.mark.integration
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
            assert job.next_run_time > datetime.now(timezone.utc)
            assert job.next_run_time < datetime.now(timezone.utc) + timedelta(minutes=2)
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

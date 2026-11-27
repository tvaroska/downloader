"""Integration tests for `/schedules` CRUD routes, history, and persistence."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from downloader.dependencies import (
    get_execution_storage_dependency,
    get_scheduler_dependency,
)
from downloader.main import app
from downloader.models.schedule import ExecutionStatus, ScheduleExecution
from downloader.scheduler.service import SchedulerService
from downloader.scheduler.storage import ExecutionStorage


@pytest.mark.unit
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
        mock_job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
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

        assert response.status_code == 400
        assert response.json()["detail"]["error_type"] == "validation_error"

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
        mock_job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
        mock_job.kwargs = {
            "schedule_id": "sched-123",
            "url": "https://example.com",
            "format": "text",
            "headers": None,
            "created_at": datetime.now(UTC).isoformat(),
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
        mock_job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
        mock_job.kwargs = {
            "schedule_id": "sched-123",
            "url": "https://example.com",
            "format": "markdown",
            "headers": {"Authorization": "Bearer token"},
            "created_at": datetime.now(UTC).isoformat(),
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
        mock_job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
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
        mock_job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
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


@pytest.mark.unit
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
            started_at=datetime.now(UTC) - timedelta(minutes=5),
            completed_at=datetime.now(UTC) - timedelta(minutes=4),
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

"""Mock fixtures for external dependencies."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.downloader.dependencies import (
    get_execution_storage_dependency,
    get_http_client,
    get_job_manager_dependency,
    get_pdf_generator_dependency,
    get_scheduler_dependency,
)
from src.downloader.main import app
from src.downloader.models.schedule import ExecutionStatus, ScheduleExecution


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client."""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """
    Fixture to mock the HTTP client with default successful response.

    Returns a mock HTTP client that simulates a successful download
    of HTML content from example.com.
    """
    mock_client = AsyncMock()
    mock_client.download.return_value = (
        b"<html>test</html>",
        {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "size": 17,
            "headers": {"content-type": "text/html"},
        },
    )
    return mock_client


@pytest.fixture
def override_http_client(mock_http_client):
    """
    Fixture to override the HTTP client dependency with a mock.

    Use this fixture when testing endpoints that use HTTPClientDep.
    """

    async def mock_get_http_client():
        return mock_http_client

    app.dependency_overrides[get_http_client] = mock_get_http_client
    yield mock_http_client
    app.dependency_overrides.pop(get_http_client, None)


@pytest.fixture
def override_job_manager(mock_job_manager):
    """
    Fixture to override the job manager dependency with a mock.

    Use this fixture when testing batch endpoints.
    """

    async def mock_get_job_manager():
        return mock_job_manager

    app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
    yield mock_job_manager
    app.dependency_overrides.pop(get_job_manager_dependency, None)


@pytest.fixture
def override_pdf_generator():
    """
    Fixture to override the PDF generator dependency with a mock.
    """
    mock_generator = AsyncMock()
    mock_generator.generate_pdf.return_value = b"%PDF-1.4 mock content"

    async def mock_get_pdf_generator():
        return mock_generator

    app.dependency_overrides[get_pdf_generator_dependency] = mock_get_pdf_generator
    yield mock_generator
    app.dependency_overrides.pop(get_pdf_generator_dependency, None)


@pytest.fixture
def mock_job_manager():
    """Fixture to mock the JobManager."""
    mock_manager = AsyncMock()
    mock_manager.create_job.return_value = "test_job_id"
    return mock_manager


@pytest.fixture
def mock_playwright():
    """Mock playwright instance for PDF generation tests.

    Creates unique browser mocks for each launch() call to ensure BrowserPool's
    set-based storage works correctly (the set stores unique objects).
    """
    with patch("src.downloader.browser.manager.async_playwright") as mock:
        playwright_instance = AsyncMock()
        mock.return_value.start = AsyncMock(return_value=playwright_instance)

        # Track all browser instances for assertions
        browsers = []

        def create_browser():
            browser = AsyncMock()
            browser.is_connected.return_value = True
            browsers.append(browser)
            return browser

        # Create a wrapper that tracks the first browser for backward compatibility
        first_browser = create_browser()

        async def mock_launch(*args, **kwargs):
            if len(browsers) == 1:
                return first_browser
            return create_browser()

        playwright_instance.chromium.launch = AsyncMock(side_effect=mock_launch)

        # Yield first_browser for backward compatibility with existing tests
        yield mock, playwright_instance, first_browser


@pytest.fixture
def mock_browser_pool():
    """Mock browser pool for PDF generation tests."""
    # Patch both locations for backward compatibility
    with patch("src.downloader.pdf_generator.BrowserPool") as mock_pool_class:
        pool_instance = AsyncMock()
        mock_pool_class.return_value = pool_instance

        browser = AsyncMock()
        pool_instance.get_browser = AsyncMock(return_value=browser)
        pool_instance.release_browser = AsyncMock()
        pool_instance.create_context = AsyncMock()

        yield pool_instance, browser


# ============= Scheduler Fixtures =============


@pytest.fixture
def mock_scheduler_service():
    """Mock SchedulerService for testing routes."""
    mock_svc = MagicMock()
    mock_svc.is_running.return_value = True
    mock_svc.get_jobs.return_value = []
    mock_svc.get_job.return_value = None
    mock_svc.remove_job = MagicMock()
    mock_svc._scheduler = MagicMock()

    # The scheduler property returns the underlying _scheduler
    type(mock_svc).scheduler = property(lambda self: self._scheduler)
    return mock_svc


@pytest.fixture
def mock_execution_storage():
    """Mock ExecutionStorage for testing routes and executor."""
    mock_storage = AsyncMock()
    mock_storage.store_execution = AsyncMock()
    mock_storage.get_execution = AsyncMock(return_value=None)
    mock_storage.get_executions = AsyncMock(return_value=[])
    mock_storage.get_execution_count = AsyncMock(return_value=0)
    mock_storage.delete_executions = AsyncMock(return_value=0)
    return mock_storage


@pytest.fixture
def mock_apscheduler_job():
    """Mock APScheduler Job object for testing."""
    job = MagicMock()
    job.id = "test-schedule-id"
    job.name = "Test Schedule"
    job.next_run_time = datetime.now(timezone.utc) + timedelta(hours=1)
    job.kwargs = {
        "schedule_id": "test-schedule-id",
        "url": "https://example.com",
        "format": "text",
        "headers": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    job.trigger = MagicMock()
    # Simulating APScheduler CronTrigger fields (month, day, week, day_of_week, hour, minute, second)
    minute_field = MagicMock()
    minute_field.__str__ = lambda self: "0"
    hour_field = MagicMock()
    hour_field.__str__ = lambda self: "9"
    day_field = MagicMock()
    day_field.__str__ = lambda self: "*"
    month_field = MagicMock()
    month_field.__str__ = lambda self: "*"
    day_of_week_field = MagicMock()
    day_of_week_field.__str__ = lambda self: "*"
    job.trigger.fields = [
        MagicMock(),
        day_of_week_field,
        MagicMock(),
        month_field,
        day_field,
        hour_field,
        minute_field,
    ]
    job.pause = MagicMock()
    return job


@pytest.fixture
def sample_schedule_execution():
    """Sample ScheduleExecution for testing (COMPLETED status)."""
    return ScheduleExecution(
        execution_id="exec-123",
        schedule_id="schedule-456",
        status=ExecutionStatus.COMPLETED,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        completed_at=datetime.now(timezone.utc),
        duration=5.0,
        success=True,
        content_size=1024,
        error_message=None,
        attempt=1,
    )


@pytest.fixture
def sample_failed_execution():
    """Sample failed ScheduleExecution for testing (FAILED status)."""
    return ScheduleExecution(
        execution_id="exec-789",
        schedule_id="schedule-456",
        status=ExecutionStatus.FAILED,
        started_at=datetime.now(timezone.utc) - timedelta(seconds=2),
        completed_at=datetime.now(timezone.utc),
        duration=2.0,
        success=False,
        content_size=None,
        error_message="HTTPClientError: HTTP 404: Not Found",
        attempt=3,
    )


@pytest.fixture
def override_scheduler_service(mock_scheduler_service):
    """Override scheduler dependency for route testing."""

    async def mock_get_scheduler():
        return mock_scheduler_service

    app.dependency_overrides[get_scheduler_dependency] = mock_get_scheduler
    yield mock_scheduler_service
    app.dependency_overrides.pop(get_scheduler_dependency, None)


@pytest.fixture
def override_execution_storage(mock_execution_storage):
    """Override execution storage dependency for route testing."""

    async def mock_get_storage():
        return mock_execution_storage

    app.dependency_overrides[get_execution_storage_dependency] = mock_get_storage
    yield mock_execution_storage
    app.dependency_overrides.pop(get_execution_storage_dependency, None)

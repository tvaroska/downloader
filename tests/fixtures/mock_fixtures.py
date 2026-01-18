"""Mock fixtures for external dependencies."""

from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
    get_pdf_generator_dependency,
)
from src.downloader.main import app


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
    with patch("src.downloader.pdf_generator.async_playwright") as mock:
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
    with patch("src.downloader.pdf_generator.BrowserPool") as mock_pool_class:
        pool_instance = AsyncMock()
        mock_pool_class.return_value = pool_instance

        browser = AsyncMock()
        pool_instance.get_browser = AsyncMock(return_value=browser)
        pool_instance.release_browser = AsyncMock()

        yield pool_instance, browser

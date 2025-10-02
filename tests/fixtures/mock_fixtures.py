"""Mock fixtures for external dependencies."""

from unittest.mock import AsyncMock, patch

import pytest


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
def mock_job_manager():
    """Fixture to mock the JobManager."""
    mock_manager = AsyncMock()
    mock_manager.create_job.return_value = "test_job_id"
    return mock_manager


@pytest.fixture
def mock_playwright():
    """Mock playwright instance for PDF generation tests."""
    with patch("src.downloader.pdf_generator.async_playwright") as mock:
        playwright_instance = AsyncMock()
        mock.return_value.start = AsyncMock(return_value=playwright_instance)

        browser = AsyncMock()
        playwright_instance.chromium.launch = AsyncMock(return_value=browser)

        yield mock, playwright_instance, browser


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

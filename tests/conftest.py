"""Shared test fixtures and configuration."""

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.main import app


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client."""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Fixture to mock the HTTP client with default successful response."""
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
def api_client():
    """Fixture to provide FastAPI test client."""
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Fixture to provide valid authentication headers."""
    return {"Authorization": "Bearer test-key"}


@pytest.fixture
def api_key_headers():
    """Fixture to provide valid API key headers."""
    return {"X-API-Key": "test-key"}


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


@pytest.fixture
def sample_html_content():
    """Fixture providing sample HTML content for tests."""
    return (
        b"<html><head><title>Test</title></head>"
        b"<body><h1>Hello</h1><p>World</p></body></html>"
    )


@pytest.fixture
def sample_metadata():
    """Fixture providing sample HTTP response metadata."""
    return {
        "url": "https://example.com",
        "status_code": 200,
        "content_type": "text/html",
        "size": 25,
        "headers": {"content-type": "text/html", "content-length": "25"},
    }


@pytest.fixture
def env_no_auth():
    """Fixture to clear authentication environment variables."""
    with patch.dict(os.environ, {}, clear=True):
        yield


@pytest.fixture
def env_with_auth():
    """Fixture to set authentication environment variables."""
    with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
        yield


@pytest.fixture
def env_with_redis():
    """Fixture to set Redis environment variables."""
    with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}, clear=True):
        yield


@pytest.fixture
def env_no_redis():
    """Fixture to clear Redis environment variables."""
    with patch.dict(os.environ, {}, clear=False):
        if "REDIS_URI" in os.environ:
            del os.environ["REDIS_URI"]
        yield


@pytest.fixture
def batch_request_basic():
    """Fixture providing a basic batch request."""
    return {
        "urls": [{"url": "https://example.com"}],
        "default_format": "text",
    }


@pytest.fixture
def batch_request_complex():
    """Fixture providing a complex batch request."""
    return {
        "urls": [
            {"url": "https://example1.com", "format": "text"},
            {"url": "https://example2.com", "format": "markdown"},
            {"url": "https://example3.com", "format": "json"},
        ],
        "default_format": "html",
        "concurrency_limit": 3,
        "timeout_per_url": 30,
    }

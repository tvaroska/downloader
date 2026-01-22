"""Sample data fixtures for tests."""

import pytest


@pytest.fixture
def sample_html_content():
    """Fixture providing sample HTML content for tests."""
    return b"<html><head><title>Test</title></head><body><h1>Hello</h1><p>World</p></body></html>"


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
def batch_request_basic():
    """Fixture providing a basic batch request."""
    return {
        "urls": [{"url": "https://example.com"}],
        "default_format": "text",
    }


@pytest.fixture
def batch_request_complex():
    """Fixture providing a complex batch request with multiple URLs and formats."""
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


# ============= Schedule Request Fixtures =============


@pytest.fixture
def schedule_request_basic():
    """Basic schedule creation request."""
    return {
        "name": "Test Schedule",
        "url": "https://example.com/page",
        "cron_expression": "0 9 * * *",  # Daily at 9 AM
        "format": "text",
    }


@pytest.fixture
def schedule_request_with_headers():
    """Schedule request with custom headers."""
    return {
        "name": "Authenticated Schedule",
        "url": "https://api.example.com/data",
        "cron_expression": "*/15 * * * *",  # Every 15 minutes
        "format": "json",
        "headers": {"Authorization": "Bearer token123"},
    }


@pytest.fixture
def schedule_request_disabled():
    """Schedule request with enabled=False."""
    return {
        "name": "Disabled Schedule",
        "url": "https://example.com/page",
        "cron_expression": "0 0 * * *",  # Daily at midnight
        "format": "markdown",
        "enabled": False,
    }

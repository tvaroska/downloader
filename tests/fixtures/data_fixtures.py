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

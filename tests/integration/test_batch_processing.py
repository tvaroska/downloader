"""Integration tests for batch processing core functionality."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from src.downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
)
from src.downloader.main import app


@pytest.mark.integration
class TestBatchProcessing:
    """Test core batch processing functionality."""

    def test_batch_available_with_redis(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test that batch endpoint works when job_manager is available."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] == "test_job_id"
            assert data["status"] == "pending"
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_basic_success(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test basic batch processing with successful URLs."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [
                    {"url": "https://example.com"},
                    {"url": "https://test.com", "format": "markdown"},
                ],
                "default_format": "text",
                "concurrency_limit": 2,
                "timeout_per_url": 30,
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_mixed_success_failure(
        self,
        api_client,
        mock_job_manager,
    ):
        """Test batch processing with mix of successful and failed URLs."""
        from src.downloader.http_client import HTTPClientError

        def mock_download_side_effect(url, priority=None):
            if "fail.com" in url:
                raise HTTPClientError("HTTP 404: Not Found", status_code=404)
            return (
                b"<html>Success</html>",
                {
                    "url": url,
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 19,
                    "headers": {"content-type": "text/html"},
                },
            )

        mock_client = AsyncMock()
        mock_client.download.side_effect = mock_download_side_effect

        async def mock_get_http_client():
            return mock_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [
                    {"url": "https://success.com"},
                    {"url": "https://fail.com"},
                    {"url": "https://success2.com"},
                ],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_different_formats(
        self,
        api_client,
        mock_job_manager,
        sample_html_content,
        sample_metadata,
    ):
        """Test batch processing with different output formats."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            sample_html_content,
            sample_metadata,
        )

        async def mock_get_http_client():
            return mock_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [
                    {"url": "https://example.com", "format": "text"},
                    {"url": "https://example.com", "format": "markdown"},
                    {"url": "https://example.com", "format": "html"},
                    {"url": "https://example.com", "format": "json"},
                ],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_pdf_format(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test batch processing with PDF format."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com", "format": "pdf"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_timeout_handling(
        self,
        api_client,
        mock_job_manager,
    ):
        """Test batch processing with timeout scenarios."""
        from src.downloader.http_client import HTTPTimeoutError

        mock_client = AsyncMock()
        mock_client.download.side_effect = HTTPTimeoutError("Request timed out")

        async def mock_get_http_client():
            return mock_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://slow.com"}],
                "default_format": "text",
                "timeout_per_url": 5,
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_concurrency_control(
        self,
        api_client,
        mock_job_manager,
    ):
        """Test batch processing respects concurrency limits."""

        async def mock_download(url, priority=None):
            await asyncio.sleep(0.01)
            return (
                b"<html>Content</html>",
                {
                    "url": url,
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 20,
                    "headers": {"content-type": "text/html"},
                },
            )

        mock_client = AsyncMock()
        mock_client.download.side_effect = mock_download

        async def mock_get_http_client():
            return mock_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": f"https://example{i}.com"} for i in range(5)],
                "default_format": "text",
                "concurrency_limit": 2,
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_large_content_handling(
        self,
        api_client,
        mock_job_manager,
    ):
        """Test batch processing handles large content properly."""
        large_content = b"<html>" + b"x" * 10000 + b"</html>"

        mock_client = AsyncMock()
        mock_client.download.return_value = (
            large_content,
            {
                "url": "https://large.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": len(large_content),
                "headers": {"content-type": "text/html"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://large.com"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

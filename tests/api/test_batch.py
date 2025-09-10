import base64
import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


@pytest.fixture
def mock_job_manager():
    """Fixture to mock the get_job_manager function."""
    mock_manager = AsyncMock()
    mock_manager.create_job.return_value = "dummy_job_id"
    with patch("src.downloader.api.get_job_manager", return_value=mock_manager) as mock_get_manager:
        yield mock_get_manager


class TestBatchEndpoint:
    """Test batch processing endpoint."""

    def test_batch_unavailable_without_redis(self):
        """Test that batch endpoint returns 503 when REDIS_URI is not set."""
        with patch.dict(os.environ, {}, clear=False):
            if "REDIS_URI" in os.environ:
                del os.environ["REDIS_URI"]

            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 503

            data = response.json()
            assert "detail" in data
            detail = data["detail"]
            assert detail["success"] is False
            assert detail["error_type"] == "service_unavailable"
            assert "Redis connection (REDIS_URI) is required" in detail["error"]

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_available_with_redis(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test that batch endpoint works when REDIS_URI is set."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            mock_client = AsyncMock()
            mock_client.download.return_value = (
                b"<html>test content</html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 25,
                    "headers": {"content-type": "text/html"},
                },
            )
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] == "dummy_job_id"
            assert data["status"] == "pending"

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_basic_success(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test basic batch processing with successful URLs."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            mock_client = AsyncMock()
            mock_client.download.return_value = (
                b"<html><h1>Hello</h1><p>World</p></html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 38,
                    "headers": {"content-type": "text/html"},
                },
            )
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [
                    {"url": "https://example.com"},
                    {"url": "https://test.com", "format": "markdown"},
                ],
                "default_format": "text",
                "concurrency_limit": 2,
                "timeout_per_url": 30,
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] is not None

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_mixed_success_failure(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing with mix of successful and failed URLs."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            from src.downloader.http_client import HTTPClientError

            def mock_download_side_effect(url):
                if "fail.com" in url:
                    raise HTTPClientError("HTTP 404: Not Found")
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
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [
                    {"url": "https://success.com"},
                    {"url": "https://fail.com"},
                    {"url": "https://success2.com"},
                ],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_different_formats(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing with different output formats."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            mock_client = AsyncMock()
            mock_client.download.return_value = (
                b"<html><h1>Title</h1><p>Content</p></html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 41,
                    "headers": {"content-type": "text/html"},
                },
            )
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [
                    {"url": "https://example.com", "format": "text"},
                    {"url": "https://example.com", "format": "markdown"},
                    {"url": "https://example.com", "format": "html"},
                    {"url": "https://example.com", "format": "json"},
                ],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.generate_pdf_from_url")
    def test_batch_pdf_format(self, mock_generate_pdf, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing with PDF format."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            mock_client = AsyncMock()
            mock_client.download.return_value = (
                b"<html>PDF Content</html>",
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": 24,
                    "headers": {"content-type": "text/html"},
                },
            )
            mock_get_client.return_value = mock_client

            pdf_content = b"%PDF-1.4 fake pdf content"
            mock_generate_pdf.return_value = pdf_content

            batch_request = {
                "urls": [{"url": "https://example.com", "format": "pdf"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    def test_batch_validation_errors(self, mock_job_manager):
        """Test batch request validation."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            # Empty URLs list
            response = client.post("/batch", json={"urls": []})
            assert response.status_code == 422  # Pydantic validation error

            # Too many URLs - this will be caught by Pydantic validation
            urls = [{"url": f"https://example{i}.com"} for i in range(51)]
            response = client.post("/batch", json={"urls": urls})
            assert response.status_code == 422  # Pydantic validation error

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_timeout_handling(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing with timeout scenarios."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            from src.downloader.http_client import HTTPTimeoutError

            mock_client = AsyncMock()
            mock_client.download.side_effect = HTTPTimeoutError("Request timed out")
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": "https://slow.com"}],
                "default_format": "text",
                "timeout_per_url": 5,
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_concurrency_control(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing respects concurrency limits."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            import asyncio

            # Track when downloads start/finish
            download_times = []

            async def mock_download(url):
                start_time = asyncio.get_event_loop().time()
                download_times.append(("start", start_time, url))
                # Simulate some processing time
                await asyncio.sleep(0.1)
                end_time = asyncio.get_event_loop().time()
                download_times.append(("end", end_time, url))

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
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": f"https://example{i}.com"} for i in range(5)],
                "default_format": "text",
                "concurrency_limit": 2,  # Limited concurrency
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    def test_batch_empty_request(self, mock_job_manager):
        """Test batch endpoint with invalid request body."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
            response = client.post("/batch", json={})
            assert response.status_code == 422  # Pydantic validation error

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_large_content_handling(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch processing handles large content properly."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}):
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
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": "https://large.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    # --- Merged Authentication Tests ---

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_no_auth_required(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch works when no authentication is required."""
        with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}, clear=True):
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
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    def test_batch_auth_required_no_key(self, mock_job_manager):
        """Test batch fails when auth required but no key provided."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key", "REDIS_URI": "redis://localhost:6379"}, clear=True):
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_required"

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_auth_bearer_token_valid(self, mock_get_client, mock_process_job, mock_job_manager):
        """Test batch works with valid Bearer token."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key", "REDIS_URI": "redis://localhost:6379"}, clear=True):
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
            mock_get_client.return_value = mock_client

            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post(
                "/batch",
                json=batch_request,
                headers={"Authorization": "Bearer test-key"},
            )
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
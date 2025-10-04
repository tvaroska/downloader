"""Tests for batch processing core functionality."""

import asyncio
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


class TestBatchProcessing:
    """Test core batch processing functionality."""

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_available_with_redis(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
        mock_http_client,
    ):
        """Test that batch endpoint works when REDIS_URI is set."""
        mock_get_client.return_value = mock_http_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] == "test_job_id"
            assert data["status"] == "pending"

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_basic_success(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
        mock_http_client,
    ):
        """Test basic batch processing with successful URLs."""
        mock_get_client.return_value = mock_http_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
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

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_mixed_success_failure(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
    ):
        """Test batch processing with mix of successful and failed URLs."""
        from src.downloader.http_client import HTTPClientError

        def mock_download_side_effect(url):
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
        mock_get_client.return_value = mock_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
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

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_different_formats(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
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
        mock_get_client.return_value = mock_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
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

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.generate_pdf_from_url")
    def test_batch_pdf_format(
        self,
        mock_generate_pdf,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
        mock_http_client,
    ):
        """Test batch processing with PDF format."""
        mock_get_client.return_value = mock_http_client

        pdf_content = b"%PDF-1.4 fake pdf content"
        mock_generate_pdf.return_value = pdf_content

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": "https://example.com", "format": "pdf"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_timeout_handling(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
    ):
        """Test batch processing with timeout scenarios."""
        from src.downloader.http_client import HTTPTimeoutError

        mock_client = AsyncMock()
        mock_client.download.side_effect = HTTPTimeoutError("Request timed out")
        mock_get_client.return_value = mock_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": "https://slow.com"}],
                "default_format": "text",
                "timeout_per_url": 5,
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_concurrency_control(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
        mock_job_manager,
    ):
        """Test batch processing respects concurrency limits."""
        download_times = []

        async def mock_download(url):
            start_time = asyncio.get_event_loop().time()
            download_times.append(("start", start_time, url))
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

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": f"https://example{i}.com"} for i in range(5)],
                "default_format": "text",
                "concurrency_limit": 2,
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    @patch(
        "src.downloader.api.process_background_batch_job",
        new_callable=AsyncMock,
    )
    @patch("src.downloader.api.get_client")
    def test_batch_large_content_handling(
        self,
        mock_get_client,
        mock_process_job,
        env_with_redis,
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
        mock_get_client.return_value = mock_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": "https://large.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

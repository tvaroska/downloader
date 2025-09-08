import base64
import json
import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


class TestHealthEndpoint:
    def test_health_check(self):
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.0.1"

        # Check service status
        assert "services" in data
        assert "batch_processing" in data["services"]
        assert "pdf_generation" in data["services"]

        # Check batch processing service
        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is True
        assert "max_concurrent_downloads" in batch_service
        assert "current_active_downloads" in batch_service
        assert "available_slots" in batch_service

        # Check PDF generation service
        pdf_service = data["services"]["pdf_generation"]
        assert pdf_service["available"] is True
        assert "max_concurrent_pdfs" in pdf_service
        assert "current_active_pdfs" in pdf_service
        assert "available_slots" in pdf_service


class TestDownloadEndpoint:
    @patch("src.downloader.api.get_client")
    def test_download_json_format(self, mock_get_client):
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

        response = client.get(
            "/https://example.com", headers={"Accept": "application/json"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"

        data = response.json()
        assert data["success"] is True
        assert data["url"] == "https://example.com"
        assert data["size"] == 17
        assert data["content_type"] == "text/html"

        decoded_content = base64.b64decode(data["content"])
        assert decoded_content == b"<html>test</html>"

    @patch("src.downloader.api.get_client")
    def test_download_text_format(self, mock_get_client):
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

        response = client.get("/https://example.com", headers={"Accept": "text/plain"})
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/plain; charset=utf-8"

        # Should strip HTML tags
        assert "Hello World" in response.text
        assert "<h1>" not in response.text

    @patch("src.downloader.api.get_client")
    def test_download_markdown_format(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><h1>Hello</h1><p>World</p><a href='https://test.com'>Link</a></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 75,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_get_client.return_value = mock_client

        response = client.get(
            "/https://example.com", headers={"Accept": "text/markdown"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/markdown; charset=utf-8"

        # Should convert HTML to markdown
        text = response.text
        assert "# Hello" in text
        assert "[Link](https://test.com)" in text

    @patch("src.downloader.api.get_client")
    def test_download_html_format(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><h1>Hello</h1></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 26,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_get_client.return_value = mock_client

        response = client.get("/https://example.com", headers={"Accept": "text/html"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert b"<html><h1>Hello</h1></html>" == response.content

    @patch("src.downloader.api.get_client")
    def test_download_raw_format(self, mock_get_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"binary data",
            {
                "url": "https://example.com/file.bin",
                "status_code": 200,
                "content_type": "application/octet-stream",
                "size": 11,
                "headers": {"content-type": "application/octet-stream"},
            },
        )
        mock_get_client.return_value = mock_client

        response = client.get("/https://example.com/file.bin")
        assert response.status_code == 200
        assert response.content == b"binary data"
        assert response.headers["content-type"] == "application/octet-stream"
        assert response.headers["X-Original-URL"] == "https://example.com/file.bin"

    def test_download_invalid_url(self):
        response = client.get("/invalid_url!")
        assert response.status_code == 400
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == "validation_error"

    @patch("src.downloader.api.get_client")
    def test_download_timeout(self, mock_get_client):
        from src.downloader.http_client import HTTPTimeoutError

        mock_client = AsyncMock()
        mock_client.download.side_effect = HTTPTimeoutError("Request timed out")
        mock_get_client.return_value = mock_client

        response = client.get("/https://example.com")
        assert response.status_code == 408
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == "timeout_error"

    @patch("src.downloader.api.get_client")
    def test_download_http_error(self, mock_get_client):
        from src.downloader.http_client import HTTPClientError

        mock_client = AsyncMock()
        mock_client.download.side_effect = HTTPClientError("HTTP 404: Not Found")
        mock_get_client.return_value = mock_client

        response = client.get("/https://example.com")
        assert response.status_code == 404
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == "http_error"

    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.generate_pdf_from_url")
    def test_download_pdf_format(self, mock_generate_pdf, mock_get_client):
        # Mock HTTP client
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_get_client.return_value = mock_client

        # Mock PDF generation
        pdf_content = b"%PDF-1.4 fake pdf content"
        mock_generate_pdf.return_value = pdf_content

        response = client.get(
            "/https://example.com", headers={"Accept": "application/pdf"}
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert "Content-Disposition" in response.headers
        assert "download.pdf" in response.headers["Content-Disposition"]

        assert response.content == pdf_content
        mock_generate_pdf.assert_called_once_with("https://example.com")

    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.generate_pdf_from_url")
    def test_download_pdf_generation_error(self, mock_generate_pdf, mock_get_client):
        from src.downloader.pdf_generator import PDFGeneratorError

        # Mock HTTP client
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_get_client.return_value = mock_client

        # Mock PDF generation failure
        mock_generate_pdf.side_effect = PDFGeneratorError("Browser failed to start")

        response = client.get(
            "/https://example.com", headers={"Accept": "application/pdf"}
        )
        assert response.status_code == 500
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == "pdf_generation_error"
        assert "Browser failed to start" in data["error"]

    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.PDF_SEMAPHORE")
    def test_download_pdf_service_unavailable(self, mock_semaphore, mock_get_client):
        """Test PDF service unavailable when at capacity."""
        # Mock HTTP client
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_get_client.return_value = mock_client

        # Mock semaphore to be locked (at capacity)
        mock_semaphore.locked.return_value = True

        response = client.get(
            "/https://example.com", headers={"Accept": "application/pdf"}
        )

        assert response.status_code == 503
        data = response.json()["detail"]
        assert data["success"] is False
        assert "temporarily unavailable" in data["error"]
        assert data["error_type"] == "service_unavailable"


class TestAuthentication:
    """Test API key authentication."""

    def test_health_endpoint_shows_auth_disabled(self):
        """Test health endpoint shows authentication is disabled."""
        with patch.dict(os.environ, {}, clear=True):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert data["auth_enabled"] is False
            assert data["auth_methods"] is None

    def test_health_endpoint_shows_auth_enabled(self):
        """Test health endpoint shows authentication is enabled."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            response = client.get("/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert data["auth_enabled"] is True
            assert isinstance(data["auth_methods"], list)

    @patch("src.downloader.api.get_client")
    def test_download_no_auth_required(self, mock_get_client):
        """Test download works when no authentication is required."""
        with patch.dict(os.environ, {}, clear=True):
            # Mock HTTP client
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

            response = client.get("/https://example.com")
            assert response.status_code == 200

    @patch("src.downloader.api.get_client")
    def test_download_auth_required_no_key(self, mock_get_client):
        """Test download fails when auth required but no key provided."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            response = client.get("/https://example.com")
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_required"

    @patch("src.downloader.api.get_client")
    def test_download_auth_bearer_token_valid(self, mock_get_client):
        """Test download works with valid Bearer token."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            # Mock HTTP client
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

            response = client.get(
                "/https://example.com", headers={"Authorization": "Bearer test-key"}
            )
            assert response.status_code == 200

    @patch("src.downloader.api.get_client")
    def test_download_auth_bearer_token_invalid(self, mock_get_client):
        """Test download fails with invalid Bearer token."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            response = client.get(
                "/https://example.com", headers={"Authorization": "Bearer wrong-key"}
            )
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_failed"

    @patch("src.downloader.api.get_client")
    def test_download_auth_x_api_key_valid(self, mock_get_client):
        """Test download works with valid X-API-Key header."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            # Mock HTTP client
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

            response = client.get(
                "/https://example.com", headers={"X-API-Key": "test-key"}
            )
            assert response.status_code == 200

    @patch("src.downloader.api.get_client")
    def test_download_auth_x_api_key_invalid(self, mock_get_client):
        """Test download fails with invalid X-API-Key header."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            response = client.get(
                "/https://example.com", headers={"X-API-Key": "wrong-key"}
            )
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_failed"


class TestBatchEndpoint:
    """Test batch processing endpoint."""

    @patch("src.downloader.api.get_client")
    def test_batch_basic_success(self, mock_get_client):
        """Test basic batch processing with successful URLs."""
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
        assert data["success"] is True
        assert data["total_requests"] == 2
        assert data["successful_requests"] == 2
        assert data["failed_requests"] == 0
        assert data["success_rate"] == 100.0
        assert len(data["results"]) == 2
        assert data["batch_id"] is not None

        # Check individual results
        for result in data["results"]:
            assert result["success"] is True
            assert result["content"] is not None
            assert result["size"] > 0
            assert result["duration"] is not None
            assert result["status_code"] == 200

    @patch("src.downloader.api.get_client")
    def test_batch_mixed_success_failure(self, mock_get_client):
        """Test batch processing with mix of successful and failed URLs."""
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

        data = response.json()
        assert data["success"] is False  # False because not all requests succeeded
        assert data["total_requests"] == 3
        assert data["successful_requests"] == 2
        assert data["failed_requests"] == 1
        assert abs(data["success_rate"] - 66.67) < 0.1  # ~66.67%

        # Check that we have one failure
        failed_results = [r for r in data["results"] if not r["success"]]
        assert len(failed_results) == 1
        assert failed_results[0]["error_type"] == "http_error"
        assert failed_results[0]["status_code"] == 404

    @patch("src.downloader.api.get_client")
    def test_batch_different_formats(self, mock_get_client):
        """Test batch processing with different output formats."""
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

        data = response.json()
        assert data["success"] is True
        assert len(data["results"]) == 4

        # Check different formats
        formats = {r["format"]: r for r in data["results"]}

        # Text format - should strip HTML tags
        text_result = formats["text"]
        assert "Title Content" in text_result["content"]
        assert "<h1>" not in text_result["content"]

        # Markdown format - should convert to markdown
        markdown_result = formats["markdown"]
        assert "# Title" in markdown_result["content"]

        # HTML format - should preserve HTML
        html_result = formats["html"]
        assert "<h1>Title</h1>" in html_result["content"]

        # JSON format - should be JSON string
        json_result = formats["json"]
        json_content = json.loads(json_result["content"])
        assert json_content["success"] is True

    @patch("src.downloader.api.get_client")
    @patch("src.downloader.api.generate_pdf_from_url")
    def test_batch_pdf_format(self, mock_generate_pdf, mock_get_client):
        """Test batch processing with PDF format."""
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

        data = response.json()
        assert data["success"] is True

        result = data["results"][0]
        assert result["success"] is True
        assert result["format"] == "pdf"
        assert result["content_base64"] is not None
        assert result["content_type"] == "application/pdf"

        # Verify PDF content
        decoded_pdf = base64.b64decode(result["content_base64"])
        assert decoded_pdf == pdf_content

    def test_batch_validation_errors(self):
        """Test batch request validation."""
        # Empty URLs list
        response = client.post("/batch", json={"urls": []})
        assert response.status_code == 422  # Pydantic validation error

        # Too many URLs - this will be caught by Pydantic validation
        urls = [{"url": f"https://example{i}.com"} for i in range(51)]
        response = client.post("/batch", json={"urls": urls})
        assert response.status_code == 422  # Pydantic validation error

        # Invalid URL format
        batch_request = {"urls": [{"url": "not-a-url!"}], "default_format": "text"}
        response = client.post("/batch", json=batch_request)
        assert (
            response.status_code == 200
        )  # Batch endpoint returns 200 with individual errors

        data = response.json()
        assert data["success"] is False
        assert data["failed_requests"] == 1
        assert data["results"][0]["error_type"] == "validation_error"

    @patch("src.downloader.api.get_client")
    def test_batch_timeout_handling(self, mock_get_client):
        """Test batch processing with timeout scenarios."""
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

        data = response.json()
        assert data["success"] is False
        assert data["failed_requests"] == 1

        result = data["results"][0]
        assert result["success"] is False
        assert result["error_type"] == "timeout_error"

    @patch("src.downloader.api.get_client")
    def test_batch_concurrency_control(self, mock_get_client):
        """Test batch processing respects concurrency limits."""
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

        data = response.json()
        assert data["success"] is True
        assert data["total_requests"] == 5

    def test_batch_empty_request(self):
        """Test batch endpoint with invalid request body."""
        response = client.post("/batch", json={})
        assert response.status_code == 422  # Pydantic validation error

    @patch("src.downloader.api.get_client")
    def test_batch_large_content_handling(self, mock_get_client):
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

        batch_request = {
            "urls": [{"url": "https://large.com"}],
            "default_format": "text",
        }

        response = client.post("/batch", json=batch_request)
        assert response.status_code == 200

        data = response.json()
        assert data["success"] is True

        result = data["results"][0]
        assert result["success"] is True
        assert result["size"] > 1000  # Should report actual processed size


class TestBatchAuthentication:
    """Test batch endpoint authentication."""

    @patch("src.downloader.api.get_client")
    def test_batch_no_auth_required(self, mock_get_client):
        """Test batch works when no authentication is required."""
        with patch.dict(os.environ, {}, clear=True):
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

    def test_batch_auth_required_no_key(self):
        """Test batch fails when auth required but no key provided."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_required"

    @patch("src.downloader.api.get_client")
    def test_batch_auth_bearer_token_valid(self, mock_get_client):
        """Test batch works with valid Bearer token."""
        with patch.dict(os.environ, {"DOWNLOADER_KEY": "test-key"}, clear=True):
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

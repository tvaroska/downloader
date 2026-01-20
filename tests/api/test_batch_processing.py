"""Tests for batch processing core functionality."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
)
from src.downloader.http_client import HTTPClientError, HTTPTimeoutError
from src.downloader.job_manager import JobStatus
from src.downloader.main import app
from src.downloader.models.responses import BatchRequest, BatchURLRequest
from src.downloader.pdf_generator import PDFGeneratorError
from src.downloader.routes.batch import (
    process_background_batch_job,
    process_single_url_in_batch,
)
from src.downloader.validation import URLValidationError


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


class TestProcessSingleUrlInBatch:
    """Unit tests for process_single_url_in_batch function."""

    @pytest.fixture
    def mock_http_client(self):
        """Create a mock HTTP client."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body><p>Hello World</p></body></html>",
            {
                "url": "https://example.com",
                "content_type": "text/html",
                "size": 45,
            },
        )
        return mock_client

    @pytest.fixture
    def pdf_semaphore(self):
        """Create a PDF semaphore."""
        return asyncio.Semaphore(1)

    @pytest.fixture
    def url_request(self):
        """Create a basic URL request."""
        return BatchURLRequest(url="https://example.com", format=None)

    # Format processing tests

    @pytest.mark.asyncio
    async def test_text_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful text format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="text")

        with patch("src.downloader.routes.batch._playwright_fallback_for_content") as mock_fallback:
            mock_fallback.return_value = "Hello World"

            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format="text",
                timeout=30,
                request_id="TEST-001",
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert result.success is True
        assert result.format == "text"
        assert result.content == "Hello World"
        assert result.status_code == 200
        assert result.size == len(b"Hello World")
        assert result.duration > 0

    @pytest.mark.asyncio
    async def test_markdown_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful markdown format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="markdown")

        with patch("src.downloader.routes.batch._playwright_fallback_for_content") as mock_fallback:
            mock_fallback.return_value = "# Hello World"

            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format="text",
                timeout=30,
                request_id="TEST-001",
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert result.success is True
        assert result.format == "markdown"
        assert result.content == "# Hello World"
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_html_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful HTML format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="html")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is True
        assert result.format == "html"
        assert "<html>" in result.content
        assert result.status_code == 200
        assert result.content_type == "text/html"

    @pytest.mark.asyncio
    async def test_pdf_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful PDF format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="pdf")

        async def mock_generate_pdf(url):
            return b"%PDF-1.4 test pdf content"

        with patch(
            "src.downloader.routes.batch.generate_pdf_from_url",
            side_effect=mock_generate_pdf,
        ):
            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format="text",
                timeout=30,
                request_id="TEST-001",
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert result.success is True
        assert result.format == "pdf"
        assert result.content_base64 is not None
        decoded = base64.b64decode(result.content_base64)
        assert decoded == b"%PDF-1.4 test pdf content"
        assert result.content_type == "application/pdf"
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_json_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful JSON format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="json")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is True
        assert result.format == "json"
        assert result.content_type == "application/json"
        assert result.status_code == 200

        parsed = json.loads(result.content)
        assert parsed["success"] is True
        assert "content" in parsed
        assert "metadata" in parsed

    @pytest.mark.asyncio
    async def test_raw_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful raw format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="raw")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is True
        assert result.format == "raw"
        assert result.content_base64 is not None
        decoded = base64.b64decode(result.content_base64)
        assert b"<html>" in decoded
        assert result.status_code == 200

    # Exception handling tests

    @pytest.mark.asyncio
    async def test_url_validation_error(self, mock_http_client, pdf_semaphore):
        """Test URL validation error handling."""
        url_request = BatchURLRequest(url="invalid-url", format="text")

        with patch("src.downloader.routes.batch.validate_url") as mock_validate:
            mock_validate.side_effect = URLValidationError("Invalid URL format")

            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format="text",
                timeout=30,
                request_id="TEST-001",
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert result.success is False
        assert result.error_type == "validation_error"
        assert result.status_code == 400
        assert "Invalid URL format" in result.error

    @pytest.mark.asyncio
    async def test_asyncio_timeout_error(self, pdf_semaphore):
        """Test asyncio timeout error handling."""
        url_request = BatchURLRequest(url="https://slow.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = asyncio.TimeoutError()

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=5,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is False
        assert result.error_type == "timeout_error"
        assert result.status_code == 408
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    async def test_http_timeout_error(self, pdf_semaphore):
        """Test HTTP timeout error handling."""
        url_request = BatchURLRequest(url="https://slow.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = HTTPTimeoutError("Request timed out")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is False
        assert result.error_type == "timeout_error"
        assert result.status_code == 408
        assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_http_client_error_with_status(self, pdf_semaphore):
        """Test HTTP client error with status code."""
        url_request = BatchURLRequest(url="https://notfound.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = HTTPClientError("Not Found", status_code=404)

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is False
        assert result.error_type == "http_error"
        assert result.status_code == 404

    @pytest.mark.asyncio
    async def test_http_client_error_no_status(self, pdf_semaphore):
        """Test HTTP client error without status code defaults to 502."""
        url_request = BatchURLRequest(url="https://error.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = HTTPClientError(
            "Connection failed", status_code=None
        )

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is False
        assert result.error_type == "http_error"
        assert result.status_code == 502

    @pytest.mark.asyncio
    async def test_pdf_generator_error(self, mock_http_client, pdf_semaphore):
        """Test PDF generator error handling."""
        url_request = BatchURLRequest(url="https://example.com", format="pdf")

        async def mock_generate_pdf_error(url):
            raise PDFGeneratorError("Failed to generate PDF")

        with patch(
            "src.downloader.routes.batch.generate_pdf_from_url",
            side_effect=mock_generate_pdf_error,
        ):
            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format="text",
                timeout=30,
                request_id="TEST-001",
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert result.success is False
        assert result.error_type == "pdf_generation_error"
        assert result.status_code == 500
        assert "PDF generation failed" in result.error

    @pytest.mark.asyncio
    async def test_unexpected_exception(self, pdf_semaphore):
        """Test unexpected exception handling."""
        url_request = BatchURLRequest(url="https://example.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = RuntimeError("Unexpected error")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is False
        assert result.error_type == "internal_error"
        assert result.status_code == 500
        assert result.error == "Internal server error"

    # Edge case tests

    @pytest.mark.asyncio
    async def test_format_override_from_url_request(self, mock_http_client, pdf_semaphore):
        """Test URL-level format overrides default format."""
        url_request = BatchURLRequest(url="https://example.com", format="html")

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="text",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is True
        assert result.format == "html"

    @pytest.mark.asyncio
    async def test_default_format_used_when_no_override(self, mock_http_client, pdf_semaphore):
        """Test default format is used when URL request has no format."""
        url_request = BatchURLRequest(url="https://example.com", format=None)

        result = await process_single_url_in_batch(
            url_request=url_request,
            default_format="html",
            timeout=30,
            request_id="TEST-001",
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert result.success is True
        assert result.format == "html"


class TestProcessBackgroundBatchJob:
    """Unit tests for process_background_batch_job function."""

    @pytest.fixture
    def mock_http_client(self):
        """Create a mock HTTP client."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body><p>Hello World</p></body></html>",
            {
                "url": "https://example.com",
                "content_type": "text/html",
                "size": 45,
            },
        )
        return mock_client

    @pytest.fixture
    def mock_job_manager(self):
        """Create a mock job manager."""
        mock_manager = AsyncMock()
        mock_manager.update_job_status = AsyncMock()
        return mock_manager

    @pytest.fixture
    def batch_semaphore(self):
        """Create a batch semaphore."""
        return asyncio.Semaphore(10)

    @pytest.fixture
    def pdf_semaphore(self):
        """Create a PDF semaphore."""
        return asyncio.Semaphore(1)

    @pytest.mark.asyncio
    async def test_all_urls_succeed(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test batch job with all URLs succeeding."""
        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
                BatchURLRequest(url="https://example3.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        with patch("src.downloader.routes.batch._playwright_fallback_for_content") as mock_fallback:
            mock_fallback.return_value = "content"

            results, summary = await process_background_batch_job(
                job_id="test-job-1",
                batch_request=batch_request,
                job_manager=mock_job_manager,
                batch_semaphore=batch_semaphore,
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert len(results) == 3
        assert all(r["success"] for r in results)
        assert summary["total_requests"] == 3
        assert summary["successful_requests"] == 3
        assert summary["failed_requests"] == 0
        assert summary["success_rate"] == 100.0
        assert summary["total_duration"] > 0

        mock_job_manager.update_job_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_urls_fail(self, mock_job_manager, batch_semaphore, pdf_semaphore):
        """Test batch job with all URLs failing."""
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = HTTPClientError("Not Found", status_code=404)

        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://fail1.com"),
                BatchURLRequest(url="https://fail2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-2",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert len(results) == 2
        assert all(not r["success"] for r in results)
        assert summary["total_requests"] == 2
        assert summary["successful_requests"] == 0
        assert summary["failed_requests"] == 2
        assert summary["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_job_manager, batch_semaphore, pdf_semaphore):
        """Test batch job with mix of success and failure."""

        async def mock_download(url, priority=None):
            if "fail" in url:
                raise HTTPClientError("Not Found", status_code=404)
            return (
                b"<html>Content</html>",
                {"url": url, "content_type": "text/html", "size": 20},
            )

        mock_http_client = AsyncMock()
        mock_http_client.download = mock_download

        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://success1.com"),
                BatchURLRequest(url="https://fail.com"),
                BatchURLRequest(url="https://success2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        with patch("src.downloader.routes.batch.validate_url", side_effect=lambda url: url):
            results, summary = await process_background_batch_job(
                job_id="test-job-3",
                batch_request=batch_request,
                job_manager=mock_job_manager,
                batch_semaphore=batch_semaphore,
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert len(results) == 3
        assert summary["total_requests"] == 3
        assert summary["successful_requests"] == 2
        assert summary["failed_requests"] == 1
        assert abs(summary["success_rate"] - 66.67) < 1

    @pytest.mark.asyncio
    async def test_job_status_updated(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test that job status is updated correctly."""
        batch_request = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        await process_background_batch_job(
            job_id="test-job-4",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        mock_job_manager.update_job_status.assert_called_once()
        call_args = mock_job_manager.update_job_status.call_args
        assert call_args[0][0] == "test-job-4"
        assert call_args[0][1] == JobStatus.RUNNING
        assert call_args[1]["progress"] == 100
        assert call_args[1]["processed_urls"] == 1

    @pytest.mark.asyncio
    async def test_results_aggregation(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test that results are properly aggregated."""
        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-5",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert isinstance(results, list)
        assert all(isinstance(r, dict) for r in results)
        assert all("url" in r for r in results)
        assert all("success" in r for r in results)

        assert "total_requests" in summary
        assert "successful_requests" in summary
        assert "failed_requests" in summary
        assert "success_rate" in summary
        assert "total_duration" in summary

    @pytest.mark.asyncio
    async def test_single_url_batch(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test batch job with single URL."""
        batch_request = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-6",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert len(results) == 1
        assert summary["total_requests"] == 1


class TestBatchEndpointEdgeCases:
    """Test edge cases for batch API endpoints."""

    def test_submit_batch_no_redis(self, api_client):
        """Test batch submission when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }
            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_no_redis(self, api_client):
        """Test job status when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job-id/status")
            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_not_found(self, api_client, mock_job_manager):
        """Test job status when job does not exist."""
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/nonexistent-job/status")
            assert response.status_code == 404
            data = response.json()["detail"]
            assert data["error_type"] == "job_not_found"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_exception(self, api_client, mock_job_manager):
        """Test job status when an exception occurs."""
        mock_job_manager.get_job_info.side_effect = RuntimeError("Database error")

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/status")
            assert response.status_code == 500
            data = response.json()["detail"]
            assert data["error_type"] == "job_status_error"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_no_redis(self, api_client):
        """Test job results when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job-id/results")
            assert response.status_code == 503
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_pending(self, api_client, mock_job_manager):
        """Test job results when job is still pending."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.PENDING
        job_info.results_available = False
        job_info.progress = 0
        job_info.created_at = datetime.now(timezone.utc)
        job_info.started_at = None
        job_info.completed_at = None
        job_info.total_urls = 1
        job_info.processed_urls = 0
        job_info.successful_urls = 0
        job_info.failed_urls = 0
        job_info.error_message = None
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "pending" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_failed(self, api_client, mock_job_manager):
        """Test job results when job has failed."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.FAILED
        job_info.results_available = False
        job_info.progress = 50
        job_info.error_message = "Something went wrong"
        job_info.created_at = datetime.now(timezone.utc)
        job_info.started_at = datetime.now(timezone.utc)
        job_info.completed_at = None
        job_info.total_urls = 2
        job_info.processed_urls = 1
        job_info.successful_urls = 0
        job_info.failed_urls = 1
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "failed" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_cancelled(self, api_client, mock_job_manager):
        """Test job results when job was cancelled."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.CANCELLED
        job_info.results_available = False
        job_info.progress = 25
        job_info.error_message = None
        job_info.created_at = datetime.now(timezone.utc)
        job_info.started_at = datetime.now(timezone.utc)
        job_info.completed_at = None
        job_info.total_urls = 4
        job_info.processed_urls = 1
        job_info.successful_urls = 1
        job_info.failed_urls = 0
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "cancelled" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_no_redis(self, api_client):
        """Test job cancellation when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job-id")
            assert response.status_code == 503
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_not_found(self, api_client, mock_job_manager):
        """Test job cancellation when job does not exist."""
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/nonexistent-job")
            assert response.status_code == 404
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_already_completed(self, api_client, mock_job_manager):
        """Test job cancellation when job is already completed."""
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.COMPLETED
        job_info.results_available = True
        job_info.progress = 100
        job_info.error_message = None
        job_info.created_at = datetime.now(timezone.utc)
        job_info.started_at = datetime.now(timezone.utc)
        job_info.completed_at = datetime.now(timezone.utc)
        job_info.total_urls = 2
        job_info.processed_urls = 2
        job_info.successful_urls = 2
        job_info.failed_urls = 0
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.cancel_job.return_value = False

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is False
            assert "could not be cancelled" in data["message"]
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_exception(self, api_client, mock_job_manager):
        """Test job cancellation when an exception occurs."""
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.RUNNING

        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.cancel_job.side_effect = RuntimeError("Database error")

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job")
            assert response.status_code == 500
            data = response.json()["detail"]
            assert data["error_type"] == "job_cancellation_error"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

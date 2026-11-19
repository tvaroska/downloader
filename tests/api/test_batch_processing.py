"""Tests for batch processing core functionality."""

import asyncio
import base64
import json
from datetime import UTC
from unittest.mock import AsyncMock, patch

import pytest

from downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
)
from downloader.http_client import HTTPClientError, HTTPTimeoutError
from downloader.job_manager import JobStatus
from downloader.main import app
from downloader.models.responses import BatchRequest, BatchURLRequest
from downloader.pdf_generator import PDFGeneratorError
from downloader.services.batch_processor import (
    process_background_batch_job,
    process_single_url_in_batch,
)
from downloader.validation import URLValidationError


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
            assert data["job_id"] == "test_job_id"
            mock_job_manager.create_job.assert_awaited_once()
            mock_job_manager.start_background_job.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_submit_batch_persists_sanitized_request_without_cleartext_secret(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """`POST /batch` strips cleartext `webhook_secret` in all mode and seals it in api mode (`R92`, `R113`)."""
        from downloader.config import Settings
        from downloader.worker_queue import WorkerQueue
        from tests.unit.test_v070_ops_drain_worker_otel import FakeRedis

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        prev_settings = getattr(app.state, "settings", None)
        prev_queue = getattr(app.state, "worker_queue", None)
        try:
            batch_request = {
                "urls": [
                    {"url": "https://example.com", "format": "text"},
                    {"url": "https://test.com", "format": "markdown"},
                ],
                "default_format": "text",
                "concurrency_limit": 2,
                "timeout_per_url": 15,
                "webhook_url": "https://example.com/hook",
                "webhook_secret": "super-secret-hmac-key",  # pragma: allowlist secret
            }
            # In default `all` mode, secret stays in-memory only and is omitted from Redis payload.
            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            persisted_all = mock_job_manager.create_job.call_args.args[0]
            assert "webhook_secret" not in persisted_all
            assert "webhook_secret_enc" not in persisted_all
            assert "super-secret-hmac-key" not in json.dumps(persisted_all)

            # In `api` split mode, secret is sealed into `webhook_secret_enc` (never cleartext).
            mock_job_manager.create_job.reset_mock()
            split_cfg = Settings()
            split_cfg.worker.deployment_mode = "api"
            split_cfg.webhook.secret_key = "unit-test-batch-master-key"  # pragma: allowlist secret
            app.state.settings = split_cfg
            app.state.worker_queue = WorkerQueue(FakeRedis())
            response_api = api_client.post("/batch", json=batch_request)
            assert response_api.status_code == 200
            persisted_api = mock_job_manager.create_job.call_args.args[0]
            assert "webhook_secret" not in persisted_api
            assert persisted_api["webhook_secret_enc"].startswith("enc:v1:")
            assert "super-secret-hmac-key" not in json.dumps(persisted_api)
        finally:
            app.state.settings = prev_settings
            app.state.worker_queue = prev_queue
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
        assert result.content is not None
        assert "Hello World" in result.content
        assert result.status_code == 200
        assert result.size == len(result.content.encode("utf-8"))
        assert result.duration is not None and result.duration > 0

    @pytest.mark.asyncio
    async def test_markdown_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful markdown format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="markdown")

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
        assert result.content is not None and "Hello World" in result.content
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
        assert result.content is not None and "<html>" in result.content
        assert result.status_code == 200
        assert result.content_type == "text/html"

    @pytest.mark.asyncio
    async def test_pdf_format_success(self, mock_http_client, pdf_semaphore):
        """Test successful PDF format processing."""
        url_request = BatchURLRequest(url="https://example.com", format="pdf")

        async def mock_generate_pdf(url, **_kwargs):
            return b"%PDF-1.4 test pdf content"

        with patch(
            "downloader.pdf_generator.generate_pdf_from_url",
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

        assert result.content is not None
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

        with patch(
            "downloader.services.batch_processor.validate_url_async", new_callable=AsyncMock
        ) as mock_validate:
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
        assert result.error is not None and "Invalid URL format" in result.error

    @pytest.mark.asyncio
    async def test_asyncio_timeout_error(self, pdf_semaphore):
        """Test asyncio timeout error handling."""
        url_request = BatchURLRequest(url="https://slow.com", format="text")
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = TimeoutError()

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
        assert result.error is not None and "timeout" in result.error.lower()

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
        assert result.error is not None and "timed out" in result.error.lower()

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

        async def mock_generate_pdf_error(url, **_kwargs):
            raise PDFGeneratorError("Failed to generate PDF")

        with patch(
            "downloader.pdf_generator.generate_pdf_from_url",
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
        assert result.error is not None and "PDF generation failed" in result.error

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

        assert mock_job_manager.update_job_status.call_count == 3
        final_kwargs = mock_job_manager.update_job_status.call_args.kwargs
        assert final_kwargs["progress"] == 100
        assert final_kwargs["processed_urls"] == 3
        assert final_kwargs["successful_urls"] == 3
        assert final_kwargs["failed_urls"] == 0

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

        async def mock_download(url, priority=None, **_kwargs):
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

        with patch(
            "downloader.services.batch_processor.validate_url_async",
            new_callable=AsyncMock,
            side_effect=lambda url, *_args: url,
        ):
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
            urls=[
                BatchURLRequest(url="https://example.com"),
                BatchURLRequest(url="https://test.com"),
                BatchURLRequest(url="https://success.com"),
            ],
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

        assert mock_job_manager.update_job_status.call_count == 3
        final_kwargs = mock_job_manager.update_job_status.call_args.kwargs
        assert final_kwargs["progress"] == 100
        assert final_kwargs["processed_urls"] == 3
        assert final_kwargs["successful_urls"] == 3
        assert final_kwargs["failed_urls"] == 0
        call_args = mock_job_manager.update_job_status.call_args
        assert call_args[0][0] == "test-job-4"
        assert call_args[0][1] == JobStatus.RUNNING

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

    @pytest.mark.asyncio
    async def test_concurrency_limit_bounds_in_flight(self, mock_job_manager, pdf_semaphore):
        """Both per-job `concurrency_limit` and global `batch_semaphore` bound concurrent fetches (`R92`)."""
        active = 0
        peak = 0

        async def _counting_download(url: str, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return (
                b"<html><body>ok</body></html>",
                {"url": url, "content_type": "text/html", "size": 28},
            )

        counting_http = AsyncMock()
        counting_http.download.side_effect = _counting_download

        req = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(6)],
            default_format="html",
            concurrency_limit=2,
            timeout_per_url=10,
        )
        results, summary = await process_background_batch_job(
            job_id="job-conc-1",
            batch_request=req,
            job_manager=mock_job_manager,
            batch_semaphore=asyncio.Semaphore(10),
            http_client=counting_http,
            pdf_semaphore=pdf_semaphore,
        )
        assert summary["successful_requests"] == 6
        assert len(results) == 6
        assert peak == 2

        # Global batch_semaphore=1 must further cap concurrency even when concurrency_limit=5.
        peak = 0
        req_wide = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(4)],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=10,
        )
        await process_background_batch_job(
            job_id="job-conc-2",
            batch_request=req_wide,
            job_manager=mock_job_manager,
            batch_semaphore=asyncio.Semaphore(1),
            http_client=counting_http,
            pdf_semaphore=pdf_semaphore,
        )
        assert peak == 1

    @pytest.mark.asyncio
    async def test_timeout_per_url_is_enforced(
        self, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """A URL exceeding `timeout_per_url` fails with `408 timeout_error` while peers succeed (`R92`)."""

        async def _slow_or_fast(url: str, **_kwargs):
            if "example1" in url:
                await asyncio.sleep(5.0)
            return (
                b"<html><body>fast</body></html>",
                {"url": url, "content_type": "text/html", "size": 30},
            )

        http_client = AsyncMock()
        http_client.download.side_effect = _slow_or_fast

        req = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
            ],
            default_format="html",
            concurrency_limit=2,
            timeout_per_url=5,
        )
        # Bypass Pydantic `ge=5` floor in-test so `asyncio.wait_for` triggers quickly.
        object.__setattr__(req, "timeout_per_url", 0.05)
        results, summary = await process_background_batch_job(
            job_id="job-timeout",
            batch_request=req,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=http_client,
            pdf_semaphore=pdf_semaphore,
        )
        assert summary["successful_requests"] == 1
        assert summary["failed_requests"] == 1
        by_url = {r["url"]: r for r in results}
        assert by_url["https://example1.com"]["success"] is False
        assert by_url["https://example1.com"]["status_code"] == 408
        assert by_url["https://example1.com"]["error_type"] == "timeout_error"
        assert by_url["https://example2.com"]["success"] is True

    @pytest.mark.asyncio
    async def test_cancelled_job_stops_before_next_url(self, batch_semaphore, pdf_semaphore):
        """A job marked CANCELLED in storage halts before starting remaining URLs (`R92`)."""
        from tests.fixtures.in_memory_ports import InMemoryJobStore

        store = InMemoryJobStore()
        job_id = await store.create_job(
            {"urls": [{"url": f"https://example{i}.com"} for i in range(4)]}
        )
        await store.update_job_status(job_id, JobStatus.RUNNING)

        fetched: list[str] = []

        async def _cancel_after_first(url: str, **_kwargs):
            fetched.append(url)
            if len(fetched) == 1:
                await store.update_job_status(job_id, JobStatus.CANCELLED)
            return (
                b"<html><body>ok</body></html>",
                {"url": url, "content_type": "text/html", "size": 28},
            )

        http_client = AsyncMock()
        http_client.download.side_effect = _cancel_after_first

        req = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(4)],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=10,
        )
        with pytest.raises(asyncio.CancelledError):
            await process_background_batch_job(
                job_id=job_id,
                batch_request=req,
                job_manager=store,
                batch_semaphore=batch_semaphore,
                http_client=http_client,
                pdf_semaphore=pdf_semaphore,
            )
        assert fetched == ["https://example0.com"]

    @pytest.mark.asyncio
    async def test_crawl_delay_outside_semaphore_and_progress_redis_error_tolerated(
        self, pdf_semaphore
    ):
        """Crawl-delay sleep runs outside `batch_semaphore` (`R114`) and Redis progress errors don't abort (`R85`)."""
        import redis.exceptions as redis_exc

        batch_sem = asyncio.Semaphore(1)
        sem_value_during_throttle: list[int] = []

        class _SpyThrottle:
            async def wait_if_needed(self, _domain: str, _delay: float | None) -> float:
                sem_value_during_throttle.append(batch_sem._value)
                return 0.0

        class _SpyRobots:
            async def check_allowed(self, _url: str, _ua: str | None = None) -> float | None:
                return 0.25

        http_client = AsyncMock()
        http_client.download.return_value = (
            b"<html><body>ok</body></html>",
            {"url": "https://example.com", "content_type": "text/html", "size": 28},
        )
        flaky_jm = AsyncMock()
        flaky_jm.get_job_info = AsyncMock(return_value=None)
        flaky_jm.update_job_status = AsyncMock(
            side_effect=redis_exc.ConnectionError("transient progress write error")
        )

        req = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=10,
        )
        from typing import Any, cast

        results, summary = await process_background_batch_job(
            job_id="job-resilient",
            batch_request=req,
            job_manager=flaky_jm,
            batch_semaphore=batch_sem,
            http_client=http_client,
            pdf_semaphore=pdf_semaphore,
            robots_checker=cast(Any, _SpyRobots()),
            domain_throttle=cast(Any, _SpyThrottle()),
        )
        assert sem_value_during_throttle == [1]
        assert summary["successful_requests"] == 1
        assert len(results) == 1


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
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.PENDING
        job_info.results_available = False
        job_info.progress = 0
        job_info.created_at = datetime.now(UTC)
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
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.FAILED
        job_info.results_available = False
        job_info.progress = 50
        job_info.error_message = "Something went wrong"
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
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
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.CANCELLED
        job_info.results_available = False
        job_info.progress = 25
        job_info.error_message = None
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
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
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.COMPLETED
        job_info.results_available = True
        job_info.progress = 100
        job_info.error_message = None
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
        job_info.completed_at = datetime.now(UTC)
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


@pytest.mark.asyncio
async def test_batch_result_aggregate_size_is_capped():
    """Aggregate batch results are bounded so a single Redis SETEX cannot grow unboundedly (R43)."""
    from datetime import datetime

    from downloader.job_manager import JobInfo, JobManager

    mock_client = AsyncMock()
    mock_client.download.return_value = (
        b"<html><body>" + b"A" * 500 + b"</body></html>",
        {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "size": 526,
            "headers": {"content-type": "text/html"},
        },
    )
    mock_jm = AsyncMock()
    mock_jm.get_job_info = AsyncMock(return_value=None)
    mock_jm.update_job_status = AsyncMock()

    batch_request = BatchRequest(
        urls=[
            BatchURLRequest(url="https://example1.com"),
            BatchURLRequest(url="https://example2.com"),
            BatchURLRequest(url="https://example3.com"),
        ],
        default_format="text",
        concurrency_limit=1,
        timeout_per_url=10,
    )

    results, summary = await process_background_batch_job(
        job_id="cap-job-1",
        batch_request=batch_request,
        job_manager=mock_jm,
        batch_semaphore=asyncio.Semaphore(1),
        http_client=mock_client,
        pdf_semaphore=asyncio.Semaphore(1),
        max_result_bytes=1200,
    )

    assert len(results) == 3
    assert any(r["error_type"] == "content_too_large" and r["status_code"] == 413 for r in results)
    assert summary["failed_requests"] >= 1

    # Also verify JobManager.store_job_results enforces the cap before SETEX
    kv: dict[str, str] = {}
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(
        return_value=JobInfo(
            job_id="cap-job-1",
            status=JobStatus.RUNNING,
            created_at=datetime.now(UTC),
            request_data={},
        ).model_dump_json()
    )
    redis_mock.setex = AsyncMock(side_effect=lambda k, _ttl, v: kv.__setitem__(k, v))
    jm = JobManager("redis://localhost:6379")
    jm.redis_client = redis_mock

    big_results = [
        {"url": f"https://example{i}.com", "success": True, "content": "X" * 400, "size": 400}
        for i in range(3)
    ]
    await jm.store_job_results(
        "cap-job-1",
        big_results,
        {"total_requests": 3},
        status=JobStatus.COMPLETED,
        total_duration=0.5,
        max_result_bytes=1100,
    )
    stored_payload = kv["job_result:cap-job-1"]
    assert len(stored_payload.encode("utf-8")) <= 1100
    parsed = json.loads(stored_payload)
    assert parsed["summary"]["truncated"] is True

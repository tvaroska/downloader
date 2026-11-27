"""Unit tests for single-URL processing within batch jobs."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import pytest

from downloader.http_client import HTTPClientError, HTTPTimeoutError
from downloader.models.responses import BatchURLRequest
from downloader.pdf_generator import PDFGeneratorError
from downloader.services.batch_processor import (
    process_single_url_in_batch,
)
from downloader.validation import URLValidationError


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

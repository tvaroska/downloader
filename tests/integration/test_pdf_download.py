"""Integration tests for PDF download functionality."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.dependencies import get_http_client, get_pdf_semaphore
from src.downloader.main import app
from src.downloader.pdf_generator import PDFGeneratorError


@pytest.mark.integration
@pytest.mark.requires_playwright
class TestPDFDownload:
    """Test PDF download integration."""

    def test_download_pdf_format(self, api_client):
        """Test downloading a URL as PDF format."""
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

        pdf_content = b"%PDF-1.4 fake pdf content"

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                return_value=pdf_content,
            ) as mock_generate_pdf:
                response = api_client.get(
                    "/https://example.com", headers={"Accept": "application/pdf"}
                )
                assert response.status_code == 200
                assert response.headers["content-type"] == "application/pdf"
                assert "Content-Disposition" in response.headers
                assert "download.pdf" in response.headers["Content-Disposition"]

                assert response.content == pdf_content
                mock_generate_pdf.assert_called_once_with("https://example.com")
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_pdf_generation_error(self, api_client):
        """Test PDF generation error handling."""
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

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                side_effect=PDFGeneratorError("Browser failed to start"),
            ):
                response = api_client.get(
                    "/https://example.com", headers={"Accept": "application/pdf"}
                )
                assert response.status_code == 500
                data = response.json()["detail"]
                assert data["success"] is False
                assert data["error_type"] == "pdf_generation_error"
                assert "Browser failed to start" in data["error"]
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_pdf_service_unavailable(self, api_client):
        """Test PDF service unavailable when at capacity."""
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

        # Create a locked semaphore (0 permits = immediately locked)
        locked_semaphore = asyncio.Semaphore(0)

        async def mock_get_http_client():
            return mock_client

        def mock_get_pdf_semaphore():
            return locked_semaphore

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
        try:
            response = api_client.get("/https://example.com", headers={"Accept": "application/pdf"})

            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["success"] is False
            assert "temporarily unavailable" in data["error"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_pdf_semaphore, None)

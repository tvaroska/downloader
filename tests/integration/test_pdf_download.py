"""Integration tests for PDF download functionality."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


@pytest.mark.integration
@pytest.mark.requires_playwright
class TestPDFDownload:
    """Test PDF download integration."""

    def test_download_pdf_format(self):
        """Test downloading a URL as PDF format."""
        with (
            patch("src.downloader.api.get_client") as mock_get_client,
            patch("src.downloader.api.generate_pdf_from_url") as mock_generate_pdf,
        ):
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

            response = client.get("/https://example.com", headers={"Accept": "application/pdf"})
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            assert "Content-Disposition" in response.headers
            assert "download.pdf" in response.headers["Content-Disposition"]

            assert response.content == pdf_content
            mock_generate_pdf.assert_called_once_with("https://example.com")

    def test_download_pdf_generation_error(self):
        """Test PDF generation error handling."""
        from src.downloader.pdf_generator import PDFGeneratorError

        with (
            patch("src.downloader.api.get_client") as mock_get_client,
            patch("src.downloader.api.generate_pdf_from_url") as mock_generate_pdf,
        ):
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

            response = client.get("/https://example.com", headers={"Accept": "application/pdf"})
            assert response.status_code == 500
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "pdf_generation_error"
            assert "Browser failed to start" in data["error"]

    def test_download_pdf_service_unavailable(self):
        """Test PDF service unavailable when at capacity."""
        with (
            patch("src.downloader.api.get_client") as mock_get_client,
            patch("src.downloader.api.PDF_SEMAPHORE") as mock_semaphore,
        ):
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

            response = client.get("/https://example.com", headers={"Accept": "application/pdf"})

            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["success"] is False
            assert "temporarily unavailable" in data["error"]
            assert data["error_type"] == "service_unavailable"

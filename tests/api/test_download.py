import base64
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.http_client import HTTPClientError, HTTPTimeoutError
from src.downloader.main import app

client = TestClient(app)


@pytest.fixture
def mock_http_client():
    """Fixture to mock the HTTP client."""
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
    with patch("src.downloader.api.get_client", return_value=mock_client) as mock_get_client:
        yield mock_get_client


class TestDownloadEndpoint:
    @pytest.mark.parametrize(
        "accept_header, mock_content, expected_content_type, expected_in_response",
        [
            (
                "application/json",
                b"<html>test</html>",
                "application/json",
                b"<html>test</html>",
            ),
            (
                "text/plain",
                b"<html><h1>Hello</h1><p>World</p></html>",
                "text/plain; charset=utf-8",
                "Hello World",
            ),
            (
                "text/markdown",
                b"<html><h1>Hello</h1><p>World</p><a href='https://test.com'>Link</a></html>",
                "text/markdown; charset=utf-8",
                "# Hello",
            ),
            (
                "text/html",
                b"<html><h1>Hello</h1></html>",
                "text/html; charset=utf-8",
                b"<html><h1>Hello</h1></html>",
            ),
        ],
    )
    def test_download_formats(
        self,
        accept_header,
        mock_content,
        expected_content_type,
        expected_in_response,
    ):
        with patch("src.downloader.api.get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.download.return_value = (
                mock_content,
                {
                    "url": "https://example.com",
                    "status_code": 200,
                    "content_type": "text/html",
                    "size": len(mock_content),
                    "headers": {"content-type": "text/html"},
                },
            )
            mock_get_client.return_value = mock_client

        response = client.get("/https://example.com", headers={"Accept": accept_header})
        assert response.status_code == 200
        assert response.headers["content-type"] == expected_content_type

        if accept_header == "application/json":
            data = response.json()
            assert data["success"] is True
            decoded_content = base64.b64decode(data["content"])
            assert decoded_content == expected_in_response
        elif isinstance(expected_in_response, bytes):
            assert response.content == expected_in_response
        else:
            assert expected_in_response in response.text

    def test_download_raw_format(self):
        with patch("src.downloader.api.get_client") as mock_get_client:
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

    @pytest.mark.parametrize(
        "error, expected_status, expected_error_type",
        [
            (HTTPTimeoutError("Request timed out"), 408, "timeout_error"),
            (HTTPClientError("HTTP 404: Not Found"), 404, "http_error"),
        ],
    )
    def test_download_client_errors(self, error, expected_status, expected_error_type):
        with patch("src.downloader.api.get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.download.side_effect = error
            mock_get_client.return_value = mock_client

            response = client.get("/https://example.com")
            assert response.status_code == expected_status
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == expected_error_type

    def test_download_pdf_format(self):
        with patch("src.downloader.api.get_client") as mock_get_client, \
             patch("src.downloader.api.generate_pdf_from_url") as mock_generate_pdf:
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

    def test_download_pdf_generation_error(self):
        from src.downloader.pdf_generator import PDFGeneratorError

        with patch("src.downloader.api.get_client") as mock_get_client, \
             patch("src.downloader.api.generate_pdf_from_url") as mock_generate_pdf:
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

    def test_download_pdf_service_unavailable(self):
        """Test PDF service unavailable when at capacity."""
        with patch("src.downloader.api.get_client") as mock_get_client, \
             patch("src.downloader.api.PDF_SEMAPHORE") as mock_semaphore:
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

    # --- Authentication Tests ---

    def test_download_no_auth_required(self, env_no_auth, mock_http_client):
        """Test download works when no authentication is required."""
        with patch("src.downloader.api.get_client", return_value=mock_http_client):
            response = client.get("/https://example.com")
            assert response.status_code == 200

    @pytest.mark.parametrize(
        "headers, expected_status, expected_error_type",
        [
            (None, 401, "authentication_required"),
            ({"Authorization": "Bearer wrong-key"}, 401, "authentication_failed"),
            ({"X-API-Key": "wrong-key"}, 401, "authentication_failed"),
        ],
    )
    def test_download_auth_failed(self, env_with_auth, headers, expected_status, expected_error_type):
        """Test download fails with missing or invalid API key."""
        response = client.get("/https://example.com", headers=headers)
        assert response.status_code == expected_status
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == expected_error_type

    @pytest.mark.parametrize(
        "headers",
        [
            ({"Authorization": "Bearer test-key"}),
            ({"X-API-Key": "test-key"}),
        ],
    )
    def test_download_auth_valid(self, env_with_auth, mock_http_client, headers):
        """Test download works with valid API key."""
        with patch("src.downloader.api.get_client", return_value=mock_http_client):
            response = client.get("/https://example.com", headers=headers)
            assert response.status_code == 200

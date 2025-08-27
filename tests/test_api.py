import pytest
import base64
import json
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
            "/https://example.com",
            headers={"Accept": "application/json"}
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

        response = client.get(
            "/https://example.com",
            headers={"Accept": "text/plain"}
        )
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
            "/https://example.com",
            headers={"Accept": "text/markdown"}
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

        response = client.get(
            "/https://example.com",
            headers={"Accept": "text/html"}
        )
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

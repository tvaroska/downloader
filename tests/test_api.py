import pytest
import base64
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
        assert "timestamp" in data
        assert data["version"] == "0.1.0"


class TestDownloadEndpoint:
    @patch("src.downloader.routers.downloader.http_client.download_content")
    def test_download_success(self, mock_download):
        mock_download.return_value = {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "content_length": "100",
            "content": b"<html>test</html>",
            "headers": {"content-type": "text/html"}
        }
        
        response = client.get("/download?url=https://example.com")
        assert response.status_code == 200
        
        data = response.json()
        assert data["url"] == "https://example.com"
        assert data["status_code"] == 200
        assert data["content_type"] == "text/html"
        
        decoded_content = base64.b64decode(data["content_base64"])
        assert decoded_content == b"<html>test</html>"

    def test_download_invalid_url(self):
        response = client.get("/download?url=")
        assert response.status_code == 400

    @patch("src.downloader.routers.downloader.http_client.download_content")
    def test_download_raw_success(self, mock_download):
        mock_download.return_value = {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "content_length": "100",
            "content": b"<html>test</html>",
            "headers": {"content-type": "text/html"}
        }
        
        response = client.get("/download/raw?url=https://example.com")
        assert response.status_code == 200
        assert response.content == b"<html>test</html>"
        assert response.headers["content-type"] == "text/html"
        assert "X-Original-URL" in response.headers
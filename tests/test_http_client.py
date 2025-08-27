import pytest
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import httpx

from src.downloader.clients.http_client import HTTPClient


class TestHTTPClient:
    @pytest.fixture
    def http_client(self):
        return HTTPClient(timeout=5.0)

    @pytest.mark.asyncio
    async def test_successful_download(self, http_client):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.headers = {
            "content-type": "text/html",
            "content-length": "100"
        }
        mock_response.content = b"<html>test</html>"
        mock_response.raise_for_status = AsyncMock()

        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                return_value=mock_response
            )
            
            result = await http_client.download_content("https://example.com")
            
            assert result["status_code"] == 200
            assert result["url"] == "https://example.com"
            assert result["content_type"] == "text/html"
            assert result["content"] == b"<html>test</html>"

    @pytest.mark.asyncio
    async def test_timeout_error(self, http_client):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.TimeoutException("Timeout")
            )
            
            with pytest.raises(HTTPException) as exc_info:
                await http_client.download_content("https://example.com")
            
            assert exc_info.value.status_code == 408
            assert "timeout" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_http_status_error(self, http_client):
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"
        
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "404 Not Found", 
                    request=AsyncMock(), 
                    response=mock_response
                )
            )
            
            with pytest.raises(HTTPException) as exc_info:
                await http_client.download_content("https://example.com")
            
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_request_error(self, http_client):
        with patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=httpx.RequestError("Connection failed")
            )
            
            with pytest.raises(HTTPException) as exc_info:
                await http_client.download_content("https://example.com")
            
            assert exc_info.value.status_code == 503
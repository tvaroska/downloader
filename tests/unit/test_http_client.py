"""Unit tests for HTTP client."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.downloader.http_client import (
    HTTPClient,
    HTTPClientError,
    HTTPTimeoutError,
    close_client,
    get_client,
)


@pytest.mark.unit
class TestHTTPClient:
    @pytest.fixture
    def http_client(self):
        return HTTPClient(timeout=5.0, max_redirects=5, user_agent="TestAgent/1.0")

    @pytest.mark.asyncio
    async def test_successful_download(self, http_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com"
        mock_response.headers = MagicMock()
        mock_response.headers.get.return_value = "text/html"
        mock_response.headers.__iter__ = lambda self: iter(
            [("content-type", "text/html"), ("content-length", "100")]
        )
        mock_response.headers.items.return_value = [
            ("content-type", "text/html"),
            ("content-length", "100"),
        ]
        mock_response.content = b"<html>test</html>"
        mock_response.reason_phrase = "OK"

        with patch.object(http_client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            content, metadata = await http_client.download("https://example.com")

            assert content == b"<html>test</html>"
            assert metadata["status_code"] == 200
            assert metadata["url"] == "https://example.com"
            assert metadata["content_type"] == "text/html"
            assert metadata["size"] == 17

    @pytest.mark.asyncio
    async def test_timeout_error(self, http_client):
        with patch.object(http_client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Timeout")

            with pytest.raises(HTTPTimeoutError) as exc_info:
                await http_client.download("https://example.com")

            assert "timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_error_response(self, http_client):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.reason_phrase = "Not Found"

        with patch.object(http_client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "HTTP 404" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_request_error(self, http_client):
        with patch.object(http_client._client, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.RequestError("Connection failed")

            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "Request failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with HTTPClient() as client:
            assert client is not None

    @pytest.mark.asyncio
    async def test_global_client(self):
        client1 = await get_client()
        client2 = await get_client()
        assert client1 is client2  # Should be same instance

        await close_client()

        client3 = await get_client()
        assert client3 is not client1  # Should be new instance

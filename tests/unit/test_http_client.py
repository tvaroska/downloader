"""Unit tests for HTTP client."""

import httpx
import pytest

from src.downloader.http_client import (
    HTTPClient,
    HTTPClientError,
    HTTPTimeoutError,
    close_client,
    get_client,
)


def _attach_mock_transport(http_client: HTTPClient, handler) -> None:
    http_client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=http_client.max_redirects,
        event_hooks={"response": [http_client._on_response_hook]},
    )


@pytest.mark.unit
class TestHTTPClient:
    @pytest.fixture
    def http_client(self):
        return HTTPClient(timeout=5.0, max_redirects=5, user_agent="TestAgent/1.0")

    @pytest.mark.asyncio
    async def test_successful_download(self, http_client):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html>test</html>",
                request=request,
            )

        _attach_mock_transport(http_client, handler)
        try:
            content, metadata = await http_client.download("https://example.com")

            assert content == b"<html>test</html>"
            assert metadata["status_code"] == 200
            assert metadata["url"] == "https://example.com"
            assert metadata["content_type"] == "text/html"
            assert metadata["size"] == 17
        finally:
            await http_client.close()

    @pytest.mark.asyncio
    async def test_timeout_error(self, http_client):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timeout", request=request)

        _attach_mock_transport(http_client, handler)
        try:
            with pytest.raises(HTTPTimeoutError) as exc_info:
                await http_client.download("https://example.com")

            assert "timed out" in str(exc_info.value)
        finally:
            await http_client.close()

    @pytest.mark.asyncio
    async def test_http_error_response(self, http_client):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"Not Found", request=request)

        _attach_mock_transport(http_client, handler)
        try:
            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "HTTP 404" in str(exc_info.value)
        finally:
            await http_client.close()

    @pytest.mark.asyncio
    async def test_request_error(self, http_client):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RequestError("Connection failed", request=request)

        _attach_mock_transport(http_client, handler)
        try:
            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "Request failed" in str(exc_info.value)
        finally:
            await http_client.close()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with HTTPClient() as client:
            assert client is not None

    @pytest.mark.asyncio
    async def test_global_client(self):
        client1 = await get_client()
        client2 = await get_client()
        assert client1 is client2

        await close_client()

        client3 = await get_client()
        assert client3 is not client1
        await close_client()

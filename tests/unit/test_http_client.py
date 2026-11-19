"""Unit tests for HTTP client."""

import httpx
import pytest

from downloader.http_client import (
    HTTPClient,
    HTTPClientError,
    HTTPTimeoutError,
)


@pytest.mark.unit
class TestHTTPClient:
    @pytest.mark.asyncio
    async def test_successful_download(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                stream=httpx.ByteStream(b"<html>test</html>"),
                request=request,
            )

        async with HTTPClient(
            timeout=5.0,
            max_redirects=5,
            user_agent="TestAgent/1.0",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            content, metadata = await http_client.download("https://example.com")

            assert content == b"<html>test</html>"
            assert metadata["status_code"] == 200
            assert metadata["url"] == "https://example.com"
            assert metadata["content_type"] == "text/html"
            assert metadata["size"] == 17

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("Timeout", request=request)

        async with HTTPClient(
            timeout=5.0,
            max_redirects=5,
            user_agent="TestAgent/1.0",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            with pytest.raises(HTTPTimeoutError) as exc_info:
                await http_client.download("https://example.com")

            assert "timed out" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_http_error_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                stream=httpx.ByteStream(b"Not Found"),
                request=request,
            )

        async with HTTPClient(
            timeout=5.0,
            max_redirects=5,
            user_agent="TestAgent/1.0",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "HTTP 404" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_request_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.RequestError("Connection failed", request=request)

        async with HTTPClient(
            timeout=5.0,
            max_redirects=5,
            user_agent="TestAgent/1.0",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            with pytest.raises(HTTPClientError) as exc_info:
                await http_client.download("https://example.com")

            assert "Request failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with HTTPClient() as client:
            assert client is not None

    @pytest.mark.asyncio
    async def test_injected_transport(self):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200, stream=httpx.ByteStream(b"injected"), request=request
            )
        )
        async with HTTPClient(transport=transport) as client:
            body, meta = await client.download("https://example.com")
            assert body == b"injected"
            assert meta["status_code"] == 200

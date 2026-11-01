"""Unit tests for HTTP streaming limits, redirect SSRF guard, rate limiting, and JSON access logs."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import Limiter
from slowapi.util import get_remote_address

from src.downloader.config import reload_settings
from src.downloader.dependencies import get_http_client, get_robots_checker
from src.downloader.http_client import ContentTooLargeError, HTTPClient
from src.downloader.logging_config import CustomJsonFormatter
from src.downloader.main import app
from src.downloader.models.responses import BatchURLRequest
from src.downloader.ratelimit_middleware import RateLimitMiddleware
from src.downloader.routes.batch import process_single_url_in_batch
from src.downloader.validation import SSRFProtectionError


class _CountingByteStream(httpx.AsyncByteStream):
    """Async byte stream tracking how many chunks were consumed before abort."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.chunks_yielded += 1
            yield chunk


@pytest.mark.unit
class TestHTTPStreamingAndRedirectSSRF:
    """Tests for HTTPClient streaming max_download_size and redirect SSRF validation."""

    async def test_content_length_exceeding_max_download_size_aborts_before_body_read(
        self,
    ) -> None:
        body_stream = _CountingByteStream([b"x" * 64, b"y" * 64])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Length": "5000", "Content-Type": "text/plain"},
                stream=body_stream,
                request=request,
            )

        client = HTTPClient(max_download_size=1024)
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(ContentTooLargeError) as exc_info:
                await client.download("https://example.com/oversized-header")

            assert exc_info.value.max_size == 1024
            assert exc_info.value.actual_size == 5000
            assert body_stream.chunks_yielded == 0
        finally:
            await client.close()

    async def test_chunked_stream_exceeding_max_download_size_aborts_mid_stream(self) -> None:
        # 5 chunks of 300 bytes = 1500 bytes > 1000 max; should abort on chunk #4 (1200 bytes)
        body_stream = _CountingByteStream([b"a" * 300 for _ in range(5)])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream"},
                stream=body_stream,
                request=request,
            )

        client = HTTPClient(max_download_size=1000)
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(ContentTooLargeError) as exc_info:
                await client.download("https://example.com/chunked-oversized")

            assert exc_info.value.max_size == 1000
            assert exc_info.value.actual_size == 1200
            # Trailing 5th chunk is never read into memory
            assert body_stream.chunks_yielded == 4
        finally:
            await client.close()

    def test_content_too_large_maps_to_http_413_on_get_url(self, api_client: TestClient) -> None:
        mock_http = AsyncMock()
        mock_http.download.side_effect = ContentTooLargeError(max_size=1024, actual_size=4096)
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            resp = api_client.get("/https://example.com/huge.bin")
            assert resp.status_code == 413
            detail = resp.json()["detail"]
            assert detail["success"] is False
            assert detail["error_type"] == "content_too_large"
            assert "1024" in detail["error"]
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    async def test_content_too_large_maps_to_413_in_batch_item(self) -> None:
        mock_http = AsyncMock()
        mock_http.download.side_effect = ContentTooLargeError(max_size=2048, actual_size=8192)
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        res = await process_single_url_in_batch(
            url_request=BatchURLRequest(url="https://example.com/huge", format="text"),
            default_format="text",
            timeout=10,
            request_id="req-413",
            http_client=mock_http,
            pdf_semaphore=asyncio.Semaphore(1),
            robots_checker=mock_robots,
        )
        assert res.success is False
        assert res.status_code == 413
        assert res.error_type == "content_too_large"

    @pytest.mark.parametrize(
        "redirect_target",
        [
            "http://169.254.169.254/latest/meta-data",
            "http://127.0.0.1:8080/internal",
            "//10.0.0.1/private-admin",
        ],
    )
    async def test_redirect_ssrf_targets_blocked(self, redirect_target: str) -> None:
        hop_targets_reached: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            hop_targets_reached.append(str(request.url))
            if str(request.url) == "https://example.com/start":
                return httpx.Response(
                    302,
                    headers={"Location": redirect_target},
                    request=request,
                )
            return httpx.Response(200, content=b"secret", request=request)

        client = HTTPClient()
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(SSRFProtectionError):
                await client.download("https://example.com/start")

            # Private/metadata redirect target was never fetched!
            assert hop_targets_reached == ["https://example.com/start"]
        finally:
            await client.close()

    async def test_safe_public_redirect_succeeds(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://example.com/old":
                return httpx.Response(
                    301,
                    headers={"Location": "/new-destination"},
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html>Redirected safely</html>",
                request=request,
            )

        client = HTTPClient()
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            content, meta = await client.download("https://example.com/old")
            assert content == b"<html>Redirected safely</html>"
            assert meta["url"] == "https://example.com/new-destination"
        finally:
            await client.close()


@pytest.mark.unit
class TestRateLimitMiddlewareEnforcement:
    """Tests for active RateLimitMiddleware 429 enforcement, headers, and path matching."""

    def test_download_path_matches_download_limit(self) -> None:
        mini_app = FastAPI()
        limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
        mw = RateLimitMiddleware(mini_app, limiter)

        settings = mw._active_settings()
        assert (
            mw._get_rate_limit_for_path("/https://example.com") == settings.ratelimit.download_limit
        )
        assert (
            mw._get_rate_limit_for_path("/https://example.com/path/to/article")
            == settings.ratelimit.download_limit
        )
        assert mw._get_rate_limit_for_path("/health") == settings.ratelimit.status_limit
        assert mw._get_rate_limit_for_path("/metrics/live") == settings.ratelimit.status_limit
        assert mw._get_rate_limit_for_path("/batch") == settings.ratelimit.batch_limit
        assert mw._get_rate_limit_for_path("/jobs/abc/status") == settings.ratelimit.batch_limit
        assert mw._get_rate_limit_for_path("/schedules") == settings.ratelimit.default_limit

    def test_enforces_http_429_retry_after_and_ratelimit_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATELIMIT_ENABLED", "true")
        monkeypatch.setenv("RATELIMIT_DOWNLOAD_LIMIT", "2/minute")
        monkeypatch.setenv("RATELIMIT_HEADERS_ENABLED", "true")
        monkeypatch.delenv("REDIS_URI", raising=False)
        reload_settings()

        mock_http = AsyncMock()
        mock_http.download.return_value = (
            b"<html>ok</html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 15,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                r1 = client.get("/https://example.com")
                assert r1.status_code == 200
                assert r1.headers["X-RateLimit-Limit"] == "2"
                assert r1.headers["X-RateLimit-Remaining"] == "1"
                assert int(r1.headers["X-RateLimit-Reset"]) > 0

                r2 = client.get("/https://example.com")
                assert r2.status_code == 200
                assert r2.headers["X-RateLimit-Remaining"] == "0"

                # 3rd request within window triggers HTTP 429 + Retry-After
                r3 = client.get("/https://example.com")
                assert r3.status_code == 429
                assert int(r3.headers["Retry-After"]) >= 1
                assert r3.headers["X-RateLimit-Limit"] == "2"
                assert r3.headers["X-RateLimit-Remaining"] == "0"

                detail = r3.json()["detail"]
                assert detail["success"] is False
                assert detail["error_type"] == "rate_limit_exceeded"

            # Starting a new TestClient lifespan resets in-memory rate-limit counters
            with TestClient(app, raise_server_exceptions=False) as fresh_client:
                r_fresh = fresh_client.get("/https://example.com")
                assert r_fresh.status_code == 200
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            monkeypatch.delenv("RATELIMIT_DOWNLOAD_LIMIT", raising=False)
            reload_settings()


@pytest.mark.unit
class TestCustomJsonFormatterAccessFields:
    """Tests for top-level path, method, and status_code extraction on uvicorn.access."""

    def test_uvicorn_access_tuple_args_extracts_top_level_fields(self) -> None:
        formatter = CustomJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=("127.0.0.1:51234", "GET", "/health?probe=readiness", "1.1", 200),
            exc_info=None,
        )
        payload = json.loads(formatter.format(record))
        assert payload["logger"] == "uvicorn.access"
        assert payload["path"] == "/health"
        assert payload["method"] == "GET"
        assert payload["status_code"] == 200

    def test_uvicorn_access_regex_fallback_extracts_top_level_fields(self) -> None:
        formatter = CustomJsonFormatter(
            "%(timestamp)s %(level)s %(logger)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        record = logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='10.0.0.5:44210 - "GET /health HTTP/1.1" 200',
            args=(),
            exc_info=None,
        )
        payload = json.loads(formatter.format(record))
        assert payload["path"] == "/health"
        assert payload["method"] == "GET"
        assert payload["status_code"] == 200

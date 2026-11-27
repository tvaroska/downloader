"""Unit and route tests for HTTPClient cross-origin redirect policy handling (`R88`)."""

from __future__ import annotations

import httpx
import pytest

from downloader.crawling.throttle import CrawlDelayExceededError
from downloader.dependencies import get_domain_throttle, get_http_client, get_robots_checker
from downloader.http_client import HTTPClient
from downloader.main import app


@pytest.mark.unit
@pytest.mark.asyncio
async def test_redirect_policy_crawl_delay_maps_to_429(api_client, env_no_auth) -> None:
    """`CrawlDelayExceededError` raised by redirect_policy propagates to HTTP 429 with Retry-After (`R88`)."""

    def _redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"Location": "https://test.com/slow-origin"},
                request=request,
            )
        return httpx.Response(
            200,
            stream=httpx.ByteStream(b"should not be fetched"),
            request=request,
        )

    async def _policy(_prev_url: str, next_url: str) -> None:
        if "test.com" in next_url:
            raise CrawlDelayExceededError(next_url, 12.5)

    # 1. Direct HTTPClient propagation (must NOT be wrapped in DownloadError)
    async with HTTPClient(
        transport=httpx.MockTransport(_redirect_handler),
        redirect_policy=_policy,
    ) as client:
        with pytest.raises(CrawlDelayExceededError) as exc_info:
            await client.download("https://example.com/start")
        assert exc_info.value.retry_after == pytest.approx(12.5)

    # 2. Full FastAPI route maps it to 429 crawl_delay_exceeded with Retry-After header
    route_http = HTTPClient(
        transport=httpx.MockTransport(_redirect_handler),
        redirect_policy=_policy,
    )

    class _AllowRobots:
        async def check_allowed(self, _url: str, _ua: str | None = None) -> float | None:
            return None

    class _NoopThrottle:
        async def wait_if_needed(self, _domain: str, _delay: float | None) -> float:
            return 0.0

    app.dependency_overrides[get_http_client] = lambda: route_http
    app.dependency_overrides[get_robots_checker] = lambda: _AllowRobots()
    app.dependency_overrides[get_domain_throttle] = lambda: _NoopThrottle()
    try:
        resp = api_client.get("/https://example.com/start", headers={"Accept": "text/plain"})
        assert resp.status_code == 429
        assert resp.json()["detail"]["error_type"] == "crawl_delay_exceeded"
        assert int(resp.headers["Retry-After"]) >= 12
    finally:
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)
        await route_http.close()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [300, 302, 304, 305])
async def test_3xx_with_no_location_maps_to_502_http_error(
    api_client, env_no_auth, status_code: int
) -> None:
    """Unfollowed `3xx` responses without `Location` raise `HTTPClientError` -> `502 http_error` (`A-11`)."""
    from downloader.http_client import HTTPClientError

    def _no_location_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            headers={"Content-Type": "text/html"},
            stream=httpx.ByteStream(b"<html>multiple choices</html>"),
            request=request,
        )

    async with HTTPClient(transport=httpx.MockTransport(_no_location_handler)) as client:
        with pytest.raises(HTTPClientError) as exc_info:
            await client.download("https://example.com/no-location")
        assert exc_info.value.status_code == status_code

    class _AllowRobots:
        async def check_allowed(self, _url: str, _ua: str | None = None) -> float | None:
            return None

    class _NoopThrottle:
        async def wait_if_needed(self, _domain: str, _delay: float | None) -> float:
            return 0.0

    route_http = HTTPClient(transport=httpx.MockTransport(_no_location_handler))
    app.dependency_overrides[get_http_client] = lambda: route_http
    app.dependency_overrides[get_robots_checker] = lambda: _AllowRobots()
    app.dependency_overrides[get_domain_throttle] = lambda: _NoopThrottle()
    try:
        resp = api_client.get("/https://example.com/no-location", headers={"Accept": "text/plain"})
        assert resp.status_code == 502
        assert resp.json()["detail"]["error_type"] == "http_error"
    finally:
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)
        await route_http.close()

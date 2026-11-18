"""Unit tests verifying cookies never persist across HTTPClient or RobotsChecker downloads (`R6`)."""

from __future__ import annotations

import httpx
import pytest

from downloader.crawling.robots import RobotsChecker
from downloader.http_client import HTTPClient


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cookies_do_not_persist_across_downloads() -> None:
    seen_cookie_headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookie_headers.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "Set-Cookie": "session_id=caller1-secret; Path=/",
            },
            content=b"ok",
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
        await client.download("https://example.com/first")
        await client.download("https://example.com/second")
    finally:
        await client.close()

    assert seen_cookie_headers == [None, None]
    assert len(client._client.cookies.jar) == 0

    # Also verify RobotsChecker client does not store Set-Cookie across origins/fetches
    robots_cookies: list[str | None] = []

    def robots_handler(request: httpx.Request) -> httpx.Response:
        robots_cookies.append(request.headers.get("cookie"))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain",
                "Set-Cookie": "tracker=1; Path=/",
            },
            content=b"User-agent: *\nAllow: /\n",
            request=request,
        )

    checker = RobotsChecker()
    checker._http_client = httpx.AsyncClient(transport=httpx.MockTransport(robots_handler))
    try:
        await checker._default_fetch("https://example.com/robots.txt")
        await checker._default_fetch("https://example.com/robots.txt")
    finally:
        await checker.close()

    assert robots_cookies == [None, None]

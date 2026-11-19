"""Unit tests for HTTPClient cross-origin redirect header isolation (R51)."""

from __future__ import annotations

import httpx
import pytest

from downloader.http_client import HTTPClient


@pytest.mark.unit
class TestHTTPClientIsolation:
    """Verify caller-supplied custom headers are dropped on cross-origin redirects."""

    @pytest.mark.parametrize(
        "target_url, expect_forwarded",
        [
            ("https://api.example.com/final", True),
            ("https://example.org/final", False),
            ("http://api.example.com/final", False),
            ("https://api.example.com:8443/final", False),
        ],
    )
    async def test_custom_headers_not_forwarded_cross_origin(
        self, target_url: str, expect_forwarded: bool
    ) -> None:
        recorded_requests: list[httpx.Request] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            recorded_requests.append(request)
            if len(recorded_requests) == 1:
                return httpx.Response(
                    302,
                    headers={"Location": target_url},
                    request=request,
                )
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"ok"),
                headers={"content-type": "text/plain"},
                request=request,
            )

        async with HTTPClient(transport=httpx.MockTransport(_handler)) as client:
            body, _meta = await client.download(
                "https://api.example.com/start",
                headers={"X-Custom-Token": "super-secret", "X-Tenant": "acme"},
            )

        assert body == b"ok"
        assert len(recorded_requests) == 2
        first_req, second_req = recorded_requests
        assert first_req.headers.get("x-custom-token") == "super-secret"
        assert first_req.headers.get("x-tenant") == "acme"

        if expect_forwarded:
            assert second_req.headers.get("x-custom-token") == "super-secret"
            assert second_req.headers.get("x-tenant") == "acme"
        else:
            assert "x-custom-token" not in second_req.headers
            assert "x-tenant" not in second_req.headers

    async def test_cookies_do_not_persist_across_downloads(self) -> None:
        """Cookies set during one download must not leak into subsequent downloads (`R6`, `R90`)."""
        seen_cookies: list[str | None] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_cookies.append(request.headers.get("cookie"))
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"hello"),
                headers={
                    "Content-Type": "text/plain",
                    "Set-Cookie": "session_id=secret-user-a; Path=/",
                },
                request=request,
            )

        async with HTTPClient(transport=httpx.MockTransport(_handler)) as client:
            body1, _ = await client.download("https://example.com/a")
            body2, _ = await client.download("https://example.com/b")

        assert body1 == b"hello"
        assert body2 == b"hello"
        assert seen_cookies == [None, None]

    async def test_cookie_set_on_redirect_is_sent_within_same_download(self) -> None:
        """A `Set-Cookie` on a 302 hop is sent on the next hop of that download only (`R90`)."""
        seen_cookies: list[tuple[str, str | None]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            cookie_hdr = request.headers.get("cookie")
            seen_cookies.append((url_str, cookie_hdr))
            if url_str.endswith("/gate"):
                if cookie_hdr and "gate_token=pass" in cookie_hdr:
                    return httpx.Response(
                        200,
                        stream=httpx.ByteStream(b"unlocked"),
                        headers={"Content-Type": "text/plain"},
                        request=request,
                    )
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://example.com/gate",
                        "Set-Cookie": "gate_token=pass; Path=/",
                    },
                    request=request,
                )
            return httpx.Response(
                200,
                stream=httpx.ByteStream(b"next"),
                headers={"Content-Type": "text/plain"},
                request=request,
            )

        async with HTTPClient(transport=httpx.MockTransport(_handler)) as client:
            body, _ = await client.download("https://example.com/gate")
            assert body == b"unlocked"
            # Subsequent independent download must NOT see `gate_token`.
            body_after, _ = await client.download("https://example.com/after")
            assert body_after == b"next"

        assert seen_cookies[0] == ("https://example.com/gate", None)
        assert seen_cookies[1][0] == "https://example.com/gate"
        assert seen_cookies[1][1] is not None and "gate_token=pass" in seen_cookies[1][1]
        assert seen_cookies[2] == ("https://example.com/after", None)

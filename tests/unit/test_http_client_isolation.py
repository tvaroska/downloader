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
                content=b"ok",
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

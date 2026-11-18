"""Unit tests verifying SSRF wiring on HTTPClient and RobotsChecker constructor ports (R17)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest

from downloader.crawling.robots import RobotsChecker
from downloader.http_client import HTTPClient
from downloader.ssrf_transport import SSRFSafeHTTPTransport, SSRFSafeNetworkBackend
from downloader.validation import SSRFProtectionError


@pytest.mark.unit
class TestSSRFWiring:
    """Tests for default SSRF transport wiring and injectable network_backend ports."""

    async def test_http_client_blocks_rebinding_between_preflight_and_connect(self) -> None:
        inner_backend = MagicMock(spec=httpcore.AsyncNetworkBackend)
        inner_backend.connect_tcp = AsyncMock()
        inner_backend.sleep = AsyncMock()

        async with HTTPClient(network_backend=inner_backend) as client:
            with (
                patch(
                    "downloader.ssrf_transport.resolve_and_validate_host_async",
                    new_callable=AsyncMock,
                    side_effect=SSRFProtectionError(
                        "Access to cloud metadata endpoints is blocked"
                    ),
                ),
                pytest.raises(SSRFProtectionError, match="metadata"),
            ):
                await client.download("https://rebind.example.com/secret")

        inner_backend.connect_tcp.assert_not_awaited()

    async def test_http_client_transport_is_ssrf_safe_and_ignores_proxy_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
        monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
        monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9999")

        async with HTTPClient() as client:
            assert client._client.trust_env is False
            assert client._client._mounts == {}
            assert isinstance(client._client._transport, SSRFSafeHTTPTransport)
            assert isinstance(
                client._client._transport._pool._network_backend, SSRFSafeNetworkBackend
            )

    async def test_robots_default_client_revalidates_redirects(self) -> None:
        def _redirect_to_metadata(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )

        checker = RobotsChecker(transport=httpx.MockTransport(_redirect_to_metadata))
        try:
            default_client = checker._ensure_http_client()
            assert default_client.trust_env is False

            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("https://example.com/article")
        finally:
            await checker.close()

        # Default construction (no transport override) wires SSRFSafeHTTPTransport
        default_checker = RobotsChecker()
        try:
            wired_client = default_checker._ensure_http_client()
            assert wired_client.trust_env is False
            assert isinstance(wired_client._transport, SSRFSafeHTTPTransport)
        finally:
            await default_checker.close()

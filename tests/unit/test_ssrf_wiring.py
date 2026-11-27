"""Unit tests verifying SSRF wiring, public transport accessors, and single EgressPolicy (`R17`, `S-12`, `T-14`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from downloader.browser.renderer import LocalBrowserRenderer
from downloader.crawling.robots import RobotsChecker
from downloader.http_client import HTTPClient
from downloader.main import app
from downloader.ssrf_transport import SSRFSafeHTTPTransport, SSRFSafeNetworkBackend
from downloader.validation import EgressPolicy, SSRFProtectionError


@pytest.mark.unit
class TestSSRFWiring:
    """Tests for default SSRF transport wiring and public transport/policy accessors."""

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
        monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/bogus-ca-bundle.pem")
        monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent/bogus-ca-dir")

        transport = SSRFSafeHTTPTransport()
        assert transport.ssl_context.cert_store_stats()["x509_ca"] > 0

        async with HTTPClient() as client:
            assert client.trust_env is False
            assert client.has_proxy_mounts is False
            assert isinstance(client.transport, SSRFSafeHTTPTransport)
            assert client.transport.ssl_context.cert_store_stats()["x509_ca"] > 0
            assert isinstance(client.transport.network_backend, SSRFSafeNetworkBackend)

    async def test_robots_default_client_revalidates_redirects(self) -> None:
        seen_urls: list[str] = []

        def _redirect_to_metadata(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )

        checker = RobotsChecker(transport=httpx.MockTransport(_redirect_to_metadata))
        try:
            assert checker.trust_env is False
            assert await checker.check_allowed("https://example.com/article") is None
            assert await checker.check_allowed("https://example.com/second") is None
            assert seen_urls == ["https://example.com/robots.txt"]
        finally:
            await checker.close()

        default_checker = RobotsChecker()
        try:
            assert default_checker.trust_env is False
            assert isinstance(default_checker.http_transport, SSRFSafeHTTPTransport)
        finally:
            await default_checker.close()

    async def test_lifespan_shares_single_egress_policy_and_renderer_forwards_policies(
        self,
    ) -> None:
        """`S-12`: Lifespan shares one `EgressPolicy` and `LocalBrowserRenderer` forwards both policies."""
        with TestClient(app):
            shared_policy = app.state.egress_policy
            assert isinstance(shared_policy, EgressPolicy)
            assert isinstance(app.state.browser_renderer, LocalBrowserRenderer)
            assert app.state.browser_renderer.egress_policy is shared_policy
            assert app.state.browser_renderer.redirect_policy is app.state.redirect_policy

        from downloader.config import Settings

        custom_egress = EgressPolicy(Settings(_env_file=None))
        redirect_hook = AsyncMock()
        renderer = LocalBrowserRenderer(
            asyncio.Semaphore(1),
            egress_policy=custom_egress,
            redirect_policy=redirect_hook,
            max_output_bytes=2_097_152,
        )
        with (
            patch(
                "downloader.pdf_generator.generate_pdf_from_url",
                new=AsyncMock(return_value=b"%PDF-1.7 ok"),
            ) as mock_pdf,
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nok"),
            ) as mock_shot,
        ):
            await renderer.render_pdf("https://example.com/doc")
            await renderer.render_screenshot("https://example.com/shot")

        assert mock_pdf.await_args is not None
        assert mock_pdf.await_args.kwargs["egress_policy"] is custom_egress
        assert mock_pdf.await_args.kwargs["redirect_policy"] is redirect_hook
        assert mock_shot.await_args is not None
        assert mock_shot.await_args.kwargs["egress_policy"] is custom_egress
        assert mock_shot.await_args.kwargs["redirect_policy"] is redirect_hook

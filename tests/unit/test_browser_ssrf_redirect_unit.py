"""Hermetic unit tests for browser redirect SSRF re-validation, service_workers block, and egress proxy (`R1`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.browser.egress_proxy import BrowserEgressProxy
from downloader.browser.manager import BrowserPool
from downloader.content_converter import render_html_with_playwright
from downloader.pdf_generator import (
    PlaywrightPDFGenerator,
    install_ssrf_route_guard,
    validate_browser_navigation,
)
from downloader.validation import SSRFProtectionError


@pytest.mark.unit
@pytest.mark.asyncio
class TestBrowserSSRFRedirectAndContextHardening:
    async def test_browser_contexts_block_service_workers(self) -> None:
        pool = BrowserPool()
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=AsyncMock())

        await pool.create_context(mock_browser, user_agent="TestUA/1.0")
        assert mock_browser.new_context.call_args.kwargs["service_workers"] == "block"

        gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0)
        fake_pool = AsyncMock()
        fake_pool.get_browser = AsyncMock(return_value=mock_browser)
        fake_pool.release_browser = AsyncMock()
        gen.pool = fake_pool
        mock_browser.new_context.reset_mock()

        async with gen._get_browser_context():
            assert mock_browser.new_context.call_args.kwargs["service_workers"] == "block"

    async def test_validate_browser_navigation_blocks_redirect_hop_to_denied_target(self) -> None:
        first_hop = SimpleNamespace(url="https://example.com/start", redirected_from=None)
        denied_hop = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            redirected_from=first_hop,
        )
        response = SimpleNamespace(
            status=200,
            url="http://169.254.169.254/latest/meta-data/",
            request=denied_hop,
        )
        page = SimpleNamespace(url="http://169.254.169.254/latest/meta-data/")

        with pytest.raises(SSRFProtectionError):
            await validate_browser_navigation(page, response)

    async def test_render_html_with_playwright_rejects_302_redirect_to_loopback(self) -> None:
        first_req = SimpleNamespace(url="https://example.com/public", redirected_from=None)
        second_req = SimpleNamespace(
            url="http://127.0.0.1:8080/internal", redirected_from=first_req
        )
        nav_response = SimpleNamespace(
            status=200,
            url="http://127.0.0.1:8080/internal",
            request=second_req,
        )

        mock_page = AsyncMock()
        mock_page.url = "http://127.0.0.1:8080/internal"
        mock_page.goto = AsyncMock(return_value=nav_response)
        mock_page.content = AsyncMock(return_value="<html>secret</html>")

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_pool = AsyncMock()
        mock_pool.get_browser = AsyncMock(return_value=MagicMock())
        mock_pool.create_context = AsyncMock(return_value=mock_context)
        mock_pool.release_browser = AsyncMock()

        mock_gen = MagicMock()
        mock_gen.pool = mock_pool
        mock_gen.ensure_started = AsyncMock(return_value=mock_pool)

        with (
            patch(
                "downloader.content_converter.get_shared_pdf_generator",
                return_value=mock_gen,
            ),
            pytest.raises(SSRFProtectionError),
        ):
            await render_html_with_playwright("https://example.com/public")

        mock_page.content.assert_not_awaited()

    async def test_subresource_redirect_violation_is_blocked_on_context(self) -> None:
        listeners: dict[str, object] = {}

        class _FakeContext:
            def __init__(self) -> None:
                self.route = AsyncMock()

            def on(self, event: str, handler: object) -> None:
                listeners[event] = handler

        ctx = _FakeContext()
        await install_ssrf_route_guard(ctx)
        assert "request" in listeners

        initial_sub = SimpleNamespace(url="https://example.com/img", redirected_from=None)
        redirected_sub = SimpleNamespace(
            url="http://100.64.0.1/internal-img",
            redirected_from=initial_sub,
        )
        handler = listeners["request"]
        assert callable(handler)
        handler(redirected_sub)

        page = SimpleNamespace(url="https://example.com/article")
        resp = SimpleNamespace(
            status=200,
            url="https://example.com/article",
            request=SimpleNamespace(url="https://example.com/article", redirected_from=None),
        )
        with pytest.raises(SSRFProtectionError):
            await validate_browser_navigation(page, resp, ctx)

    async def test_egress_proxy_blocks_http_and_connect_to_denied_addresses(self) -> None:
        proxy = BrowserEgressProxy()
        await proxy.start()
        try:
            # 1. Plain HTTP proxy request to cloud metadata IP -> 403 Forbidden
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n"
                b"Host: 169.254.169.254\r\n\r\n"
            )
            await writer.drain()
            raw_resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            assert raw_resp.startswith(b"HTTP/1.1 403 Forbidden")

            # 2. HTTPS CONNECT tunnel to shared address space 100.64.0.1 -> 403 Forbidden
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer2.write(b"CONNECT 100.64.0.1:443 HTTP/1.1\r\nHost: 100.64.0.1:443\r\n\r\n")
            await writer2.drain()
            raw_resp2 = await reader2.read(4096)
            writer2.close()
            await writer2.wait_closed()
            assert raw_resp2.startswith(b"HTTP/1.1 403 Forbidden")
        finally:
            await proxy.close()

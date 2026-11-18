"""Unit tests verifying every Playwright context factory installs the SSRF route guard (R17)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.content_converter import (
    convert_content_with_playwright_fallback,
    render_html_with_playwright,
)
from downloader.pdf_generator import (
    BrowserSession,
    PlaywrightPDFGenerator,
    _ssrf_route_guard,
)


def _make_fake_generator() -> tuple[MagicMock, AsyncMock, AsyncMock]:
    context = AsyncMock()
    context.route = AsyncMock()
    context.on = MagicMock()
    context.close = AsyncMock()

    page = AsyncMock()
    page.url = "https://example.com"
    page.goto = AsyncMock(return_value=MagicMock(status=200, request=None))
    page.wait_for_load_state = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])
    page.content = AsyncMock(return_value="<html><body><main>Rendered OK</main></body></html>")
    page.pdf = AsyncMock(return_value=b"%PDF-1.7 ok")
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\nOK")
    context.new_page = AsyncMock(return_value=page)

    browser = AsyncMock()
    browser.is_connected = MagicMock(return_value=True)
    browser.new_context = AsyncMock(return_value=context)

    pool = AsyncMock()
    pool.get_browser = AsyncMock(return_value=browser)
    pool.release_browser = AsyncMock()

    generator = MagicMock()
    generator.ensure_started = AsyncMock(return_value=pool)
    generator.user_agent = None
    generator.wait_until = "domcontentloaded"
    generator.page_load_timeout = 5000
    generator.pool = pool

    return generator, context, page


@pytest.mark.unit
class TestPlaywrightSSRFRouteGuard:
    """Verify every context creation site installs the SSRF route guard."""

    async def test_every_context_factory_installs_route_guard(self) -> None:
        # 1. Direct BrowserSession (pdf_generator.py)
        gen1, ctx1, _ = _make_fake_generator()
        async with BrowserSession(gen1) as (ctx, _page):
            assert ctx is ctx1
        ctx1.route.assert_awaited_once()
        assert ctx1.route.await_args is not None
        assert ctx1.route.await_args.args[0] == "**/*"
        assert ctx1.route.await_args.args[1] is _ssrf_route_guard

        # 2. PlaywrightPDFGenerator._get_browser_context, generate_pdf, generate_screenshot
        gen2 = PlaywrightPDFGenerator(pool_size=1)
        fake_gen2, ctx2, _ = _make_fake_generator()
        gen2.pool = fake_gen2.pool
        gen2.ensure_started = AsyncMock(return_value=fake_gen2.pool)  # type: ignore[method-assign]

        async with gen2._get_browser_context() as (_br, ctx):
            assert ctx is ctx2
        assert ctx2.route.await_count == 1

        with patch("downloader.pdf_generator.validate_url_async", new=AsyncMock()):
            pdf_bytes = await gen2.generate_pdf("https://example.com")
            assert pdf_bytes.startswith(b"%PDF")
            assert ctx2.route.await_count == 2

            shot_bytes = await gen2.generate_screenshot("https://example.com")
            assert shot_bytes.startswith(b"\x89PNG")
            assert ctx2.route.await_count == 3

        # 3. content_converter.py: convert_content_with_playwright_fallback & render_html_with_playwright
        gen3, ctx3, _ = _make_fake_generator()
        with patch("downloader.pdf_generator.validate_url_async", new=AsyncMock()):
            text = await convert_content_with_playwright_fallback(
                "https://example.com", "text", generator=gen3
            )
            assert "Rendered OK" in text
            assert ctx3.route.await_count == 1

            html = await render_html_with_playwright("https://example.com", generator=gen3)
            assert b"Rendered OK" in html
            assert ctx3.route.await_count == 2

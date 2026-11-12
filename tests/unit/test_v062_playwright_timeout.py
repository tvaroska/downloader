"""Unit tests for v0.6.2 single-pass domcontentloaded + bounded settle navigation resilience."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.downloader.config import ContentConfig, Settings
from src.downloader.content_converter import (
    convert_content_with_playwright_fallback,
    render_html_with_playwright,
)


def _make_mock_playwright():
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock(return_value=AsyncMock(status=200))
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.content = AsyncMock(
        return_value="<html><body><article><h1>Loaded</h1><p>Article body text.</p></article></body></html>"
    )

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.close = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=MagicMock())
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    mock_generator = MagicMock()
    mock_generator.ensure_started = AsyncMock(return_value=mock_pool)
    mock_generator.pool = mock_pool
    mock_generator.user_agent = None

    return mock_generator, mock_pool, mock_context, mock_page


@pytest.mark.unit
class TestPlaywrightNavigationResilience:
    """Verify F-2026-11-05-002 single-pass domcontentloaded + bounded non-fatal settle."""

    @pytest.mark.parametrize("exc_cls", [PlaywrightTimeoutError, TimeoutError])
    async def test_render_html_single_pass_survives_networkidle_settle_timeout(
        self, exc_cls: type[Exception]
    ) -> None:
        mock_gen, mock_pool, mock_context, mock_page = _make_mock_playwright()
        mock_page.wait_for_load_state.side_effect = exc_cls("tracker still open")

        with patch(
            "src.downloader.content_converter.get_shared_pdf_generator",
            return_value=mock_gen,
        ):
            html = await render_html_with_playwright("https://example.com/ad-heavy")

        assert b"Article body text." in html
        mock_page.goto.assert_awaited_once_with(
            "https://example.com/ad-heavy",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        mock_page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=2500)
        mock_context.close.assert_awaited_once()
        mock_pool.release_browser.assert_awaited_once()

    @pytest.mark.parametrize("output_format", ["text", "markdown"])
    async def test_fallback_single_pass_survives_networkidle_settle_timeout(
        self, output_format: str
    ) -> None:
        mock_gen, mock_pool, mock_context, mock_page = _make_mock_playwright()
        mock_page.wait_for_load_state.side_effect = PlaywrightTimeoutError("analytics open")

        with patch(
            "src.downloader.content_converter.get_shared_pdf_generator",
            return_value=mock_gen,
        ):
            extracted = await convert_content_with_playwright_fallback(
                "https://example.com/tracker",
                output_format=output_format,  # type: ignore[arg-type]
            )

        assert "Article body text." in extracted
        mock_page.goto.assert_awaited_once_with(
            "https://example.com/tracker",
            wait_until="domcontentloaded",
            timeout=10000,
        )
        mock_page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=2500)
        mock_context.close.assert_awaited_once()
        mock_pool.release_browser.assert_awaited_once()

    async def test_settle_timeout_clamps_to_configured_render_timeout(self) -> None:
        mock_gen, _, _, mock_page = _make_mock_playwright()
        tight_settings = Settings(content=ContentConfig(html_js_rendering_timeout=1500))

        with (
            patch("src.downloader.content_converter.get_settings", return_value=tight_settings),
            patch(
                "src.downloader.content_converter.get_shared_pdf_generator",
                return_value=mock_gen,
            ),
        ):
            await render_html_with_playwright("https://example.com/fast")

        mock_page.goto.assert_awaited_once_with(
            "https://example.com/fast",
            wait_until="domcontentloaded",
            timeout=1500,
        )
        mock_page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=1500)

    async def test_goto_domcontentloaded_timeout_raises_and_releases_browser(self) -> None:
        mock_gen, mock_pool, mock_context, mock_page = _make_mock_playwright()
        mock_page.goto.side_effect = PlaywrightTimeoutError("origin hung")

        with (
            patch(
                "src.downloader.content_converter.get_shared_pdf_generator",
                return_value=mock_gen,
            ),
            pytest.raises(PlaywrightTimeoutError),
        ):
            await render_html_with_playwright("https://example.com/hung")

        mock_page.goto.assert_awaited_once()
        mock_page.wait_for_load_state.assert_not_awaited()
        mock_context.close.assert_awaited_once()
        mock_pool.release_browser.assert_awaited_once()

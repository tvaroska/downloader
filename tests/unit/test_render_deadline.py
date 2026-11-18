"""Unit tests for R31: overall BrowserSession deadline and bounded modal closer."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader.browser.actions import BrowserAction
from downloader.pdf_generator import BrowserSession, PlaywrightPDFGenerator, close_page_modals


@pytest.mark.unit
async def test_modal_closer_is_bounded() -> None:
    """R31: close_page_modals caps total clicks and total elapsed time on pages with many modals."""
    click_count = 0

    async def _slow_click(*, timeout: int = 1000) -> None:
        nonlocal click_count
        click_count += 1
        await asyncio.sleep(0.05)

    buttons = []
    for _ in range(50):
        btn = MagicMock()
        btn.click = AsyncMock(side_effect=_slow_click)
        buttons.append(btn)

    page = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=buttons)
    page.wait_for_timeout = AsyncMock()

    # Capped by max_clicks=5 even though 50 buttons matched the first selector
    await close_page_modals(page, max_clicks=5, timeout_seconds=5.0)
    assert click_count == 5

    # Capped by timeout_seconds when clicks hang
    hang_count = 0

    async def _hanging_click(*, timeout: int = 1000) -> None:
        nonlocal hang_count
        hang_count += 1
        await asyncio.sleep(5.0)

    hanging_btn = MagicMock()
    hanging_btn.click = AsyncMock(side_effect=_hanging_click)
    hanging_page = AsyncMock()
    hanging_page.query_selector_all = AsyncMock(return_value=[hanging_btn] * 10)

    await asyncio.wait_for(
        close_page_modals(hanging_page, max_clicks=10, timeout_seconds=0.05),
        timeout=1.0,
    )
    assert hang_count == 1


@pytest.mark.unit
async def test_browser_session_enforces_overall_deadline_and_cleans_up() -> None:
    """R31: BrowserSession enforces an overall asyncio.timeout budget and releases browser."""
    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.close = AsyncMock()

    mock_browser = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    gen.ensure_started = AsyncMock(return_value=mock_pool)  # type: ignore[method-assign]

    with pytest.raises(TimeoutError):
        async with BrowserSession(gen, timeout_seconds=0.05):
            await asyncio.sleep(5.0)

    mock_context.close.assert_awaited_once()
    mock_pool.release_browser.assert_awaited_once_with(mock_browser)
    assert gen._active_contexts == 0


@pytest.mark.unit
async def test_pre_capture_skips_modal_closer_when_actions_supplied() -> None:
    """R31: close_page_modals is skipped when caller supplies explicit actions."""
    mock_page = AsyncMock()
    mock_page.goto = AsyncMock(return_value=AsyncMock(status=200))
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.click = AsyncMock()
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.pdf = AsyncMock(return_value=b"%PDF-1.7 ok")

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.close = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=MagicMock())
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    gen.ensure_started = AsyncMock(return_value=mock_pool)  # type: ignore[method-assign]

    await gen.generate_pdf(
        "https://example.com",
        actions=[BrowserAction(type="click", selector="#open-dialog")],
    )

    mock_page.click.assert_awaited_once()
    mock_page.query_selector_all.assert_not_awaited()

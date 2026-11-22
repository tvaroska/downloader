"""Unit tests for R31: overall BrowserSession deadline and bounded modal closer."""

from __future__ import annotations

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from downloader.browser.actions import BrowserAction
from downloader.browser.renderer import render_html_with_playwright
from downloader.browser.session import _make_ssrf_route_guard
from downloader.pdf_generator import BrowserSession, PlaywrightPDFGenerator, close_page_modals
from downloader.validation import SSRFProtectionError


@pytest.mark.unit
async def test_modal_closer_is_bounded() -> None:
    """R31/R115: close_page_modals caps total clicks and total elapsed time on pages with many modals."""
    click_count = 0

    async def _slow_click(*, timeout: int = 1000) -> None:
        nonlocal click_count
        assert 0 < timeout <= 1000
        click_count += 1
        await asyncio.sleep(0.05)

    buttons = []
    for _ in range(50):
        btn = MagicMock()
        btn.is_visible = AsyncMock(return_value=True)
        btn.click = AsyncMock(side_effect=_slow_click)
        buttons.append(btn)

    page = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=buttons)
    page.wait_for_timeout = AsyncMock()

    # Capped by max_clicks=5 even though 50 buttons matched the first selector
    await close_page_modals(page, max_clicks=5, timeout_seconds=5.0)
    assert click_count == 5

    # Capped by remaining monotonic budget when Playwright click respects timeout
    hang_count = 0
    seen_timeouts: list[int] = []

    async def _hanging_click(*, timeout: int = 1000) -> None:
        nonlocal hang_count
        hang_count += 1
        seen_timeouts.append(timeout)
        await asyncio.sleep(max(timeout / 1000.0, 0.01) + 0.02)
        raise PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded")

    hanging_btn = MagicMock()
    hanging_btn.is_visible = AsyncMock(return_value=True)
    hanging_btn.click = AsyncMock(side_effect=_hanging_click)
    hanging_page = AsyncMock()
    hanging_page.query_selector_all = AsyncMock(return_value=[hanging_btn] * 10)

    await asyncio.wait_for(
        close_page_modals(hanging_page, max_clicks=10, timeout_seconds=0.05),
        timeout=1.0,
    )
    assert hang_count == 1
    assert 0 < seen_timeouts[0] <= 50


@pytest.mark.unit
async def test_browser_session_enforces_overall_deadline_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R31: BrowserSession enforces an overall asyncio.timeout budget and releases browser."""
    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.on = MagicMock()
    mock_context.close = AsyncMock()

    mock_browser = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    monkeypatch.setattr(gen, "ensure_started", AsyncMock(return_value=mock_pool))

    with pytest.raises(TimeoutError):
        async with BrowserSession(gen, timeout_seconds=0.05):
            await asyncio.sleep(5.0)

    mock_context.close.assert_awaited_once()
    mock_pool.release_browser.assert_awaited_once_with(mock_browser)
    assert gen._active_contexts == 0


@pytest.mark.unit
async def test_pre_capture_skips_modal_closer_when_actions_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    mock_context.on = MagicMock()
    mock_context.close = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=MagicMock())
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    monkeypatch.setattr(gen, "ensure_started", AsyncMock(return_value=mock_pool))

    await gen.generate_pdf(
        "https://example.com",
        actions=[BrowserAction(type="click", selector="#open-dialog")],
    )

    mock_page.click.assert_awaited_once()
    mock_page.query_selector_all.assert_not_awaited()


@pytest.mark.unit
async def test_html_render_skips_modal_closer_when_actions_supplied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTML render skips `close_page_modals` when caller supplies explicit `actions` (`R96`)."""
    mock_page = AsyncMock()
    mock_page.url = "https://example.com"
    mock_page.goto = AsyncMock(return_value=AsyncMock(status=200, request=None))
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.click = AsyncMock()
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.content = AsyncMock(return_value="<html><body><main>Done</main></body></html>")

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.on = MagicMock()
    mock_context.close = AsyncMock()

    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=MagicMock())
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    monkeypatch.setattr(gen, "ensure_started", AsyncMock(return_value=mock_pool))

    rendered = await render_html_with_playwright(
        "https://example.com",
        actions=[BrowserAction(type="click", selector="#open-dialog")],
        generator=gen,
    )

    assert b"Done" in rendered
    mock_page.click.assert_awaited_once()
    mock_page.query_selector_all.assert_not_awaited()


@pytest.mark.unit
async def test_modal_closer_skips_hidden_buttons_without_burning_click_budget() -> None:
    """R115: Invisible modal close buttons are skipped via is_visible() without calling click()."""
    hidden_buttons = []
    for _ in range(8):
        hidden = MagicMock()
        hidden.is_visible = AsyncMock(return_value=False)
        hidden.click = AsyncMock(side_effect=AssertionError("hidden button must not be clicked"))
        hidden_buttons.append(hidden)

    visible_btn = MagicMock()
    visible_btn.is_visible = AsyncMock(return_value=True)
    visible_btn.click = AsyncMock()

    page = AsyncMock()
    page.query_selector_all = AsyncMock(side_effect=[hidden_buttons, [visible_btn], [], [], [], []])
    page.wait_for_timeout = AsyncMock()

    await close_page_modals(page, max_clicks=1, timeout_seconds=2.0)

    for hidden in hidden_buttons:
        hidden.is_visible.assert_awaited_once()
        hidden.click.assert_not_awaited()
    visible_btn.is_visible.assert_awaited_once()
    visible_btn.click.assert_awaited_once()
    click_timeout = visible_btn.click.call_args.kwargs["timeout"]
    assert 0 < click_timeout <= 1000


@pytest.mark.unit
async def test_modal_closer_clamps_budget_without_orphaning_rpc_future() -> None:
    """R115: Budget exhaustion clamps click/settle timeouts without asyncio.timeout cancellation."""
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    prev_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, ctx: loop_errors.append(ctx))

    recorded_click_timeouts: list[int] = []
    recorded_settle_timeouts: list[int] = []

    async def _slow_visible_click(*, timeout: int = 1000) -> None:
        recorded_click_timeouts.append(timeout)
        # Simulate Playwright's internal Channel callback future resolving via Playwright's timeout.
        rpc_future: asyncio.Future[None] = loop.create_future()

        def _reject_timeout() -> None:
            if not rpc_future.done():
                rpc_future.set_exception(PlaywrightTimeoutError(f"Timeout {timeout}ms exceeded"))

        handle = loop.call_later(0.03, _reject_timeout)
        try:
            await rpc_future
        finally:
            handle.cancel()

    async def _settle(ms: int) -> None:
        recorded_settle_timeouts.append(ms)

    btn1 = MagicMock()
    btn1.is_visible = AsyncMock(return_value=True)
    btn1.click = AsyncMock()
    btn2 = MagicMock()
    btn2.is_visible = AsyncMock(return_value=True)
    btn2.click = AsyncMock(side_effect=_slow_visible_click)
    btn3 = MagicMock()
    btn3.is_visible = AsyncMock(return_value=True)
    btn3.click = AsyncMock(side_effect=AssertionError("must exit once budget reaches <= 0"))

    page = AsyncMock()
    page.query_selector_all = AsyncMock(side_effect=[[btn1, btn2, btn3], [], [], [], [], []])
    page.wait_for_timeout = AsyncMock(side_effect=_settle)

    try:
        await close_page_modals(page, max_clicks=5, timeout_seconds=0.025)
        gc.collect()
        await asyncio.sleep(0.02)
    finally:
        loop.set_exception_handler(prev_handler)

    assert len(recorded_click_timeouts) == 1
    assert 0 < recorded_click_timeouts[0] <= 25
    assert all(0 < ms <= 25 for ms in recorded_settle_timeouts)
    btn3.click.assert_not_awaited()
    assert loop_errors == []


@pytest.mark.unit
async def test_browser_session_cleanup_unroutes_and_drains_redirect_tasks_before_close() -> None:
    """R115: BrowserSession._cleanup unroutes and cancels pending redirect tasks before ctx.close()."""
    call_order: list[str] = []
    cancelled_in_cleanup = False

    async def _hanging_redirect_check() -> None:
        nonlocal cancelled_in_cleanup
        try:
            await asyncio.sleep(10.0)
        except asyncio.CancelledError:
            cancelled_in_cleanup = True
            call_order.append("task_cancelled")
            raise

    pending_task = asyncio.create_task(_hanging_redirect_check())
    await asyncio.sleep(0)

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.on = MagicMock()

    async def _unroute_all(*, behavior: str = "default") -> None:
        assert behavior == "ignoreErrors"
        call_order.append("unroute_all")

    async def _close_ctx() -> None:
        call_order.append("close")

    mock_context.unroute_all = AsyncMock(side_effect=_unroute_all)
    mock_context.close = AsyncMock(side_effect=_close_ctx)

    mock_browser = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool

    async with BrowserSession(gen) as (ctx, _page):
        ctx._ssrf_redirect_tasks.add(pending_task)

    assert cancelled_in_cleanup is True
    assert pending_task.done()
    assert call_order == ["unroute_all", "task_cancelled", "close"]
    mock_pool.release_browser.assert_awaited_once_with(mock_browser)


@pytest.mark.unit
async def test_ssrf_route_guard_ignores_playwright_error_on_closed_context() -> None:
    """R115: _make_ssrf_route_guard swallows PlaywrightError when context closes mid-DNS lookup."""
    egress_policy = MagicMock()
    egress_policy.validate_url = AsyncMock(
        side_effect=["https://example.com", SSRFProtectionError("blocked")]
    )
    guard = _make_ssrf_route_guard(egress_policy)

    allowed_route = MagicMock()
    allowed_route.request = MagicMock(url="https://example.com/page")
    allowed_route.continue_ = AsyncMock(
        side_effect=PlaywrightError("Target page, context or browser has been closed")
    )
    allowed_route.abort = AsyncMock()
    await guard(allowed_route)
    allowed_route.continue_.assert_awaited_once()

    blocked_route = MagicMock()
    blocked_route.request = MagicMock(url="http://127.0.0.1:8080/internal")
    blocked_route.continue_ = AsyncMock()
    blocked_route.abort = AsyncMock(
        side_effect=PlaywrightError("Target page, context or browser has been closed")
    )
    await guard(blocked_route)
    blocked_route.abort.assert_awaited_once_with("accessdenied")

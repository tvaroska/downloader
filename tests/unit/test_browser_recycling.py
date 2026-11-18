"""Unit tests for BrowserPool max-use recycling, idle scale-to-zero, and Playwright SSRF guard."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.browser.manager import BrowserConfig, BrowserPool
from downloader.config import reload_settings
from downloader.pdf_generator import (
    DEFAULT_BROWSER_USER_AGENT,
    PlaywrightPDFGenerator,
    _ssrf_route_guard,
    install_ssrf_route_guard,
    resolve_browser_user_agent,
)


@pytest.mark.unit
class TestBrowserPoolMaxUsesRecycling:
    """Tests for BrowserPool recycling healthy browsers at max_uses_per_browser."""

    async def test_recycles_browser_when_usage_reaches_max_uses_per_browser(self) -> None:
        launched_browsers: list[AsyncMock] = []

        def _make_browser() -> AsyncMock:
            b = AsyncMock()
            b.is_connected = MagicMock(return_value=True)
            b.close = AsyncMock()
            launched_browsers.append(b)
            return b

        with patch("downloader.browser.manager.async_playwright") as mock_pw_fn:
            pw_instance = AsyncMock()
            pw_instance.chromium.launch = AsyncMock(side_effect=lambda **_kw: _make_browser())
            pw_instance.stop = AsyncMock()
            mock_pw_fn.return_value.start = AsyncMock(return_value=pw_instance)

            pool = BrowserPool(BrowserConfig(pool_size=1, max_uses_per_browser=2))
            await pool.start()
            try:
                assert len(launched_browsers) == 1
                first_browser = launched_browsers[0]

                # 1st use (usage_count == 1 < 2) -> retained in pool
                b1 = await pool.get_browser()
                assert b1 is first_browser
                await pool.release_browser(b1)
                assert pool._recycled_count == 0
                assert len(launched_browsers) == 1
                first_browser.close.assert_not_awaited()

                # 2nd use (usage_count == 2 >= 2) -> recycled and replaced with fresh instance
                b2 = await pool.get_browser()
                assert b2 is first_browser
                await pool.release_browser(b2)

                assert pool._recycled_count == 1
                assert pool.config.max_uses_per_browser == 2
                assert pool._available_browsers.qsize() == 1
                assert len(launched_browsers) == 2
                first_browser.close.assert_awaited_once()

                # Next caller receives the fresh replacement browser with usage_count == 1
                b3 = await pool.get_browser()
                assert b3 is launched_browsers[1]
                assert pool._browser_health[b3]["usage_count"] == 1
                await pool.release_browser(b3)
            finally:
                await pool.close()


@pytest.mark.unit
class TestPlaywrightIdleScaleToZero:
    """Tests for PlaywrightPDFGenerator idle_timeout scale-to-zero and lazy restart."""

    async def test_scales_pool_to_none_after_idle_timeout_and_restarts_lazily(self) -> None:
        created_pools: list[AsyncMock] = []

        def _make_pool(*_args, **_kwargs) -> AsyncMock:
            p = AsyncMock()
            p.start = AsyncMock()
            p.close = AsyncMock()
            browser = AsyncMock()
            browser.is_connected = MagicMock(return_value=True)
            context = AsyncMock()
            context.route = AsyncMock()
            context.close = AsyncMock()
            browser.new_context = AsyncMock(return_value=context)
            p.get_browser = AsyncMock(return_value=browser)
            p.release_browser = AsyncMock()
            created_pools.append(p)
            return p

        with patch("downloader.pdf_generator.BrowserPool", side_effect=_make_pool):
            gen = PlaywrightPDFGenerator(pool_size=2, max_uses_per_browser=25, idle_timeout=0.1)
            try:
                assert gen.pool is None

                # Lazy start on first use
                pool_1 = await gen.ensure_started()
                assert gen.pool is pool_1
                assert len(created_pools) == 1
                pool_1.start.assert_awaited_once()

                # Wait past 0.1s idle_timeout (reaper checks every 0.05s)
                for _ in range(12):
                    if gen.pool is None:
                        break
                    await asyncio.sleep(0.03)

                assert gen.pool is None
                pool_1.close.assert_awaited_once()

                # Next call lazily starts a brand-new pool
                async with gen._get_browser_context() as (_browser, _ctx):
                    assert gen.pool is not None
                    assert gen.pool is created_pools[1]
                    assert len(created_pools) == 2
                    # Active context prevents idle reaping even while sleeping > idle_timeout
                    await asyncio.sleep(0.15)
                    assert gen.pool is created_pools[1]
            finally:
                await gen.close()


@pytest.mark.unit
class TestPlaywrightSSRFRouteGuard:
    """Tests for Playwright context.route('**/*', ...) SSRF and scheme guard."""

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://127.0.0.1:8080/admin",
            "http://192.168.1.1/router",
            "file:///etc/passwd",
            "ftp://example.com/archive.tar.gz",
        ],
    )
    async def test_aborts_ssrf_and_disallowed_scheme_requests(self, blocked_url: str) -> None:
        route = AsyncMock()
        route.request = MagicMock()
        route.request.url = blocked_url

        await _ssrf_route_guard(route)

        route.abort.assert_awaited_once_with("accessdenied")
        route.continue_.assert_not_awaited()

    @pytest.mark.parametrize(
        "allowed_url",
        [
            "https://example.com/article",
            "http://test.com/styles.css",
            "data:text/html,<h1>Hello</h1>",
            "about:blank",
            "blob:https://example.com/550e8400-e29b-41d4-a716-446655440000",
        ],
    )
    async def test_continues_public_https_and_inline_schemes(self, allowed_url: str) -> None:
        route = AsyncMock()
        route.request = MagicMock()
        route.request.url = allowed_url

        await _ssrf_route_guard(route)

        route.continue_.assert_awaited_once()
        route.abort.assert_not_awaited()

    async def test_install_ssrf_route_guard_and_user_agent_propagation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        context = AsyncMock()
        await install_ssrf_route_guard(context)
        context.route.assert_awaited_once_with("**/*", _ssrf_route_guard)

        # Default browser UA when USER_AGENT is not explicitly overridden
        monkeypatch.delenv("USER_AGENT", raising=False)
        reload_settings()
        assert resolve_browser_user_agent(None) == DEFAULT_BROWSER_USER_AGENT
        assert resolve_browser_user_agent("ExplicitBot/3.0") == "ExplicitBot/3.0"

        # Explicit USER_AGENT env var propagates to browser contexts
        monkeypatch.setenv("USER_AGENT", "CustomCrawler/0.6")
        reload_settings()
        try:
            assert resolve_browser_user_agent(None) == "CustomCrawler/0.6"
        finally:
            monkeypatch.delenv("USER_AGENT", raising=False)
            reload_settings()


@pytest.mark.unit
async def test_cancel_during_context_close_still_releases_browser() -> None:
    """R28: Cancelling during ctx.close() must still release the browser and decrement active count."""
    from downloader.pdf_generator import BrowserSession

    close_started = asyncio.Event()
    close_finish = asyncio.Event()

    async def _slow_close() -> None:
        close_started.set()
        await close_finish.wait()

    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.route = AsyncMock()
    mock_context.close = AsyncMock(side_effect=_slow_close)

    mock_browser = MagicMock()
    mock_pool = MagicMock()
    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.create_context = AsyncMock(return_value=mock_context)
    mock_pool.release_browser = AsyncMock()

    gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
    gen.pool = mock_pool
    gen.ensure_started = AsyncMock(return_value=mock_pool)  # type: ignore[method-assign]

    async def _run_session() -> None:
        async with BrowserSession(gen):
            pass

    task = asyncio.create_task(_run_session())
    await close_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    close_finish.set()
    await asyncio.sleep(0)

    mock_pool.release_browser.assert_awaited_once_with(mock_browser)
    assert gen._active_contexts == 0

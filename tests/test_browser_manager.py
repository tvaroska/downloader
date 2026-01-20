"""Tests for browser pool manager."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.downloader.browser import BrowserConfig, BrowserPool, BrowserPoolError


class TestBrowserConfig:
    """Test BrowserConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = BrowserConfig()
        assert config.pool_size == 3
        assert config.memory_limit_mb == 512
        assert config.acquire_timeout == 30.0
        assert config.headless is True
        assert config.close_timeout == 5.0
        assert config.force_kill_timeout == 2.0

    def test_custom_config(self):
        """Test custom configuration values."""
        config = BrowserConfig(
            pool_size=5,
            memory_limit_mb=256,
            acquire_timeout=60.0,
            headless=False,
        )
        assert config.pool_size == 5
        assert config.memory_limit_mb == 256
        assert config.acquire_timeout == 60.0
        assert config.headless is False

    def test_memory_limit_in_launch_args(self):
        """Test memory limit flag is included in launch args."""
        config = BrowserConfig(memory_limit_mb=256)
        args = config.get_launch_args()
        assert "--js-flags=--max-old-space-size=256" in args

    def test_default_memory_limit_in_launch_args(self):
        """Test default memory limit (512MB) in launch args."""
        config = BrowserConfig()
        args = config.get_launch_args()
        assert "--js-flags=--max-old-space-size=512" in args

    def test_launch_args_contains_security_flags(self):
        """Test that standard security flags are present."""
        config = BrowserConfig()
        args = config.get_launch_args()
        assert "--no-sandbox" in args
        assert "--disable-extensions" in args
        assert "--disable-plugins" in args

    def test_launch_args_is_copy(self):
        """Test that get_launch_args returns a copy, not the original list."""
        config = BrowserConfig()
        args1 = config.get_launch_args()
        args2 = config.get_launch_args()
        assert args1 is not args2
        assert args1 == args2


class TestBrowserPoolError:
    """Test BrowserPoolError exception."""

    def test_error_message(self):
        """Test error message."""
        error = BrowserPoolError("Test error")
        assert str(error) == "Test error"

    def test_error_inheritance(self):
        """Test that BrowserPoolError inherits from Exception."""
        error = BrowserPoolError("Test")
        assert isinstance(error, Exception)


class TestBrowserPoolInit:
    """Test BrowserPool initialization."""

    def test_default_config(self):
        """Test BrowserPool uses default config when none provided."""
        pool = BrowserPool()
        assert pool.config.pool_size == 3
        assert pool.config.memory_limit_mb == 512

    def test_custom_config(self):
        """Test BrowserPool uses custom config when provided."""
        config = BrowserConfig(pool_size=5, memory_limit_mb=256)
        pool = BrowserPool(config=config)
        assert pool.config.pool_size == 5
        assert pool.config.memory_limit_mb == 256

    def test_pool_size_property(self):
        """Test pool_size property for backward compatibility."""
        config = BrowserConfig(pool_size=7)
        pool = BrowserPool(config=config)
        assert pool.pool_size == 7

    def test_initial_state(self):
        """Test initial pool state before start()."""
        pool = BrowserPool()
        assert pool._closed is False
        assert pool._playwright is None
        assert len(pool._all_browsers) == 0


class TestBrowserPoolProcessManagement:
    """Test process management and zombie cleanup."""

    def test_custom_close_timeout(self):
        """Test custom close timeout configuration."""
        config = BrowserConfig(close_timeout=10.0, force_kill_timeout=3.0)
        assert config.close_timeout == 10.0
        assert config.force_kill_timeout == 3.0

    def test_get_browser_pid_with_valid_mock(self):
        """Test PID extraction from mock browser with valid structure."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser._impl_obj._browser_process.pid = 12345

        pid = pool._get_browser_pid(mock_browser)
        assert pid == 12345

    def test_get_browser_pid_returns_none_on_missing_impl(self):
        """Test PID extraction returns None when _impl_obj is missing."""
        pool = BrowserPool()

        mock_browser = Mock(spec=[])  # No _impl_obj attribute

        pid = pool._get_browser_pid(mock_browser)
        assert pid is None

    def test_get_browser_pid_returns_none_on_missing_process(self):
        """Test PID extraction returns None when _browser_process is missing."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser._impl_obj = Mock(spec=[])  # No _browser_process attribute

        pid = pool._get_browser_pid(mock_browser)
        assert pid is None

    @pytest.mark.asyncio
    async def test_force_kill_handles_missing_process(self):
        """Test force kill handles ProcessLookupError gracefully."""
        pool = BrowserPool()
        # PID that doesn't exist - should not raise
        await pool._force_kill_process(999999999)

    @pytest.mark.asyncio
    async def test_close_browser_with_timeout_graceful_success(self):
        """Test browser closes gracefully when within timeout."""
        config = BrowserConfig(close_timeout=5.0)
        pool = BrowserPool(config)

        mock_browser = Mock()
        mock_browser.is_connected.return_value = True

        async def mock_close():
            pass

        mock_browser.close = mock_close

        result = await pool._close_browser_with_timeout(mock_browser, pid=None)
        assert result is True

    @pytest.mark.asyncio
    async def test_close_browser_with_timeout_disconnected(self):
        """Test browser close when already disconnected."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        result = await pool._close_browser_with_timeout(mock_browser, pid=None)
        # Returns False because browser wasn't connected
        assert result is False

    @pytest.mark.asyncio
    async def test_close_browser_with_timeout_exception(self):
        """Test browser close handles exceptions."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = True

        async def mock_close():
            raise Exception("Close failed")

        mock_browser.close = mock_close

        result = await pool._close_browser_with_timeout(mock_browser, pid=None)
        assert result is False

    @pytest.mark.asyncio
    async def test_force_kill_process_sigterm_then_sigkill(self):
        """Test force kill sends SIGTERM then SIGKILL."""
        config = BrowserConfig(force_kill_timeout=0.01)
        pool = BrowserPool(config)

        with patch("os.kill") as mock_kill:
            # Process survives SIGTERM, needs SIGKILL
            mock_kill.side_effect = [None, None, None]  # SIGTERM, check, SIGKILL
            await pool._force_kill_process(12345)

            # Should have called kill at least twice
            assert mock_kill.call_count >= 2

    @pytest.mark.asyncio
    async def test_force_kill_process_permission_error(self):
        """Test force kill handles PermissionError."""
        pool = BrowserPool()

        with patch("os.kill", side_effect=PermissionError("Permission denied")):
            # Should not raise
            await pool._force_kill_process(12345)


class TestBrowserPoolStart:
    """Test BrowserPool start and initialization."""

    @pytest.mark.asyncio
    async def test_start_initializes_browsers(self, mock_playwright):
        """Test start() creates the configured number of browsers."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()

        assert len(pool._all_browsers) == 1
        assert pool._available_browsers.qsize() == 1

        await pool.close()

    @pytest.mark.asyncio
    async def test_start_initializes_health_tracking(self, mock_playwright):
        """Test start() initializes health tracking for browsers."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()

        assert len(pool._browser_health) == 1
        browser = list(pool._all_browsers)[0]
        health = pool._browser_health[browser]
        assert health["usage_count"] == 0
        assert health["healthy"] is True
        assert "last_used" in health

        await pool.close()

    @pytest.mark.asyncio
    async def test_start_handles_launch_failure(self, mock_playwright):
        """Test start() raises BrowserPoolError on launch failure."""
        mock, playwright_instance, mock_browser = mock_playwright
        playwright_instance.chromium.launch = AsyncMock(side_effect=Exception("Launch failed"))

        pool = BrowserPool()

        with pytest.raises(BrowserPoolError) as exc_info:
            await pool.start()

        assert "Browser pool initialization failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_start_sets_playwright_instance(self, mock_playwright):
        """Test start() sets the playwright instance."""
        mock, playwright_instance, mock_browser = mock_playwright
        pool = BrowserPool()

        await pool.start()

        assert pool._playwright is not None
        assert pool._closed is False

        await pool.close()


class TestBrowserPoolGetBrowser:
    """Test BrowserPool.get_browser()."""

    @pytest.mark.asyncio
    async def test_get_browser_returns_from_queue(self, mock_playwright):
        """Test get_browser returns a browser from the queue."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()
        browser = await pool.get_browser()

        assert browser is not None
        assert browser in pool._all_browsers

        await pool.close()

    @pytest.mark.asyncio
    async def test_get_browser_updates_usage_count(self, mock_playwright):
        """Test get_browser increments usage count."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()
        browser = await pool.get_browser()

        assert pool._browser_health[browser]["usage_count"] == 1

        await pool.close()

    @pytest.mark.asyncio
    async def test_get_browser_updates_last_used(self, mock_playwright):
        """Test get_browser updates last_used timestamp."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()
        browser = list(pool._all_browsers)[0]
        original_last_used = pool._browser_health[browser]["last_used"]

        # Small delay to ensure timestamp changes
        await asyncio.sleep(0.01)
        await pool.get_browser()

        assert pool._browser_health[browser]["last_used"] > original_last_used

        await pool.close()

    @pytest.mark.asyncio
    async def test_get_browser_pool_closed_raises_error(self, mock_playwright):
        """Test get_browser raises error when pool is closed."""
        mock, playwright_instance, mock_browser = mock_playwright
        pool = BrowserPool()

        await pool.start()
        await pool.close()

        with pytest.raises(BrowserPoolError) as exc_info:
            await pool.get_browser()

        assert "Browser pool is closed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_get_browser_timeout(self, mock_playwright):
        """Test get_browser raises error on timeout."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1, acquire_timeout=0.1)
        pool = BrowserPool(config)

        await pool.start()

        # Get the only browser, leaving queue empty
        await pool.get_browser()

        # Try to get another - should timeout
        with pytest.raises(BrowserPoolError) as exc_info:
            await pool.get_browser()

        assert "No browser available within timeout" in str(exc_info.value)

        await pool.close()


class TestBrowserPoolReleaseBrowser:
    """Test BrowserPool.release_browser()."""

    @pytest.mark.asyncio
    async def test_release_healthy_browser_returns_to_queue(self, mock_playwright):
        """Test releasing a healthy browser returns it to the queue."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()
        browser = await pool.get_browser()
        assert pool._available_browsers.qsize() == 0

        await pool.release_browser(browser)
        assert pool._available_browsers.qsize() == 1

        await pool.close()

    @pytest.mark.asyncio
    async def test_release_unhealthy_browser_triggers_replace(self, mock_playwright):
        """Test releasing an unhealthy browser triggers replacement."""
        mock, playwright_instance, mock_browser = mock_playwright
        config = BrowserConfig(pool_size=1)
        pool = BrowserPool(config)

        await pool.start()
        browser = await pool.get_browser()

        # Mark browser as unhealthy
        browser.is_connected.return_value = False

        await pool.release_browser(browser)

        # Browser should be replaced, pool should still have one browser
        assert len(pool._all_browsers) == 1
        # The new browser should be in the queue
        assert pool._available_browsers.qsize() == 1

        await pool.close()

    @pytest.mark.asyncio
    async def test_release_closed_pool_ignored(self, mock_playwright):
        """Test releasing browser when pool is closed does nothing."""
        mock, playwright_instance, mock_browser = mock_playwright
        pool = BrowserPool()

        await pool.start()
        browser = await pool.get_browser()
        await pool.close()

        # Should not raise
        await pool.release_browser(browser)

    @pytest.mark.asyncio
    async def test_release_unknown_browser_ignored(self, mock_playwright):
        """Test releasing unknown browser does nothing."""
        mock, playwright_instance, mock_browser = mock_playwright
        pool = BrowserPool()

        await pool.start()

        unknown_browser = Mock()
        # Should not raise
        await pool.release_browser(unknown_browser)

        await pool.close()


class TestBrowserPoolHealthCheck:
    """Test browser health checking."""

    @pytest.mark.asyncio
    async def test_is_browser_healthy_connected(self):
        """Test _is_browser_healthy returns True when connected."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = True

        result = await pool._is_browser_healthy(mock_browser)
        assert result is True

    @pytest.mark.asyncio
    async def test_is_browser_healthy_disconnected(self):
        """Test _is_browser_healthy returns False when disconnected."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        result = await pool._is_browser_healthy(mock_browser)
        assert result is False

    @pytest.mark.asyncio
    async def test_is_browser_healthy_exception(self):
        """Test _is_browser_healthy returns False on exception."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.side_effect = Exception("Connection check failed")

        result = await pool._is_browser_healthy(mock_browser)
        assert result is False


class TestBrowserPoolReplaceBrowser:
    """Test browser replacement."""

    @pytest.mark.asyncio
    async def test_replace_removes_old_from_tracking(self):
        """Test _replace_browser removes old browser from tracking."""
        pool = BrowserPool()

        # Manually add a browser to simulate pool state
        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        async def mock_close():
            pass

        mock_browser.close = mock_close
        pool._all_browsers.add(mock_browser)
        pool._browser_health[mock_browser] = {"usage_count": 5, "pid": None}

        # Mock _launch_browser to return a new browser
        new_browser = Mock()
        new_browser.is_connected.return_value = True
        pool._launch_browser = AsyncMock(return_value=new_browser)

        await pool._replace_browser(mock_browser)

        assert mock_browser not in pool._all_browsers
        assert mock_browser not in pool._browser_health

    @pytest.mark.asyncio
    async def test_replace_creates_new_browser(self):
        """Test _replace_browser creates a new browser."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        async def mock_close():
            pass

        mock_browser.close = mock_close
        pool._all_browsers.add(mock_browser)
        pool._browser_health[mock_browser] = {"usage_count": 5, "pid": None}

        new_browser = Mock()
        new_browser.is_connected.return_value = True
        pool._launch_browser = AsyncMock(return_value=new_browser)

        await pool._replace_browser(mock_browser)

        # Should have the new browser
        assert new_browser in pool._all_browsers
        assert pool._browser_health[new_browser]["usage_count"] == 0

    @pytest.mark.asyncio
    async def test_replace_initializes_new_health_tracking(self):
        """Test _replace_browser initializes health tracking for new browser."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        async def mock_close():
            pass

        mock_browser.close = mock_close
        pool._all_browsers.add(mock_browser)
        pool._browser_health[mock_browser] = {"usage_count": 5, "pid": None}

        new_browser = Mock()
        new_browser.is_connected.return_value = True
        pool._launch_browser = AsyncMock(return_value=new_browser)

        await pool._replace_browser(mock_browser)

        health = pool._browser_health[new_browser]
        assert health["usage_count"] == 0
        assert health["healthy"] is True

    @pytest.mark.asyncio
    async def test_replace_handles_launch_failure(self):
        """Test _replace_browser handles launch failure gracefully."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_browser.is_connected.return_value = False

        async def mock_close():
            pass

        mock_browser.close = mock_close
        pool._all_browsers.add(mock_browser)
        pool._browser_health[mock_browser] = {"usage_count": 5, "pid": None}

        # Make launch fail
        pool._launch_browser = AsyncMock(side_effect=Exception("Launch failed"))

        # Should not raise
        await pool._replace_browser(mock_browser)

        # Old browser should be removed
        assert mock_browser not in pool._all_browsers


class TestBrowserPoolClose:
    """Test BrowserPool.close()."""

    @pytest.mark.asyncio
    async def test_close_sets_closed_flag(self):
        """Test close() sets the closed flag."""
        pool = BrowserPool()
        pool._closed = False

        await pool.close()
        assert pool._closed is True

    @pytest.mark.asyncio
    async def test_close_clears_all_browsers(self):
        """Test close() clears all browser tracking."""
        pool = BrowserPool()

        # Add mock browsers
        mock_browser1 = Mock()
        mock_browser1.is_connected.return_value = False
        mock_browser2 = Mock()
        mock_browser2.is_connected.return_value = False

        pool._all_browsers.add(mock_browser1)
        pool._all_browsers.add(mock_browser2)
        pool._browser_health[mock_browser1] = {"usage_count": 1}
        pool._browser_health[mock_browser2] = {"usage_count": 2}

        await pool.close()
        assert len(pool._all_browsers) == 0
        assert len(pool._browser_health) == 0

    @pytest.mark.asyncio
    async def test_close_drains_queue(self):
        """Test close() drains the available browsers queue."""
        pool = BrowserPool()

        # Add items to queue
        mock_browser = Mock()
        mock_browser.is_connected.return_value = False
        pool._all_browsers.add(mock_browser)
        await pool._available_browsers.put(mock_browser)

        assert pool._available_browsers.qsize() == 1

        await pool.close()
        assert pool._available_browsers.empty()

    @pytest.mark.asyncio
    async def test_close_stops_playwright(self):
        """Test close() stops the playwright instance."""
        pool = BrowserPool()

        mock_playwright_instance = AsyncMock()
        pool._playwright = mock_playwright_instance

        await pool.close()

        mock_playwright_instance.stop.assert_called_once()
        assert pool._playwright is None


class TestBrowserPoolStats:
    """Test BrowserPool.get_pool_stats()."""

    def test_get_pool_stats_available_count(self):
        """Test get_pool_stats returns correct available count."""
        pool = BrowserPool()

        # Add 3 browsers, 2 in queue (available)
        mock_browser1 = Mock()
        mock_browser2 = Mock()
        mock_browser3 = Mock()
        pool._all_browsers.add(mock_browser1)
        pool._all_browsers.add(mock_browser2)
        pool._all_browsers.add(mock_browser3)
        pool._browser_health[mock_browser1] = {"usage_count": 0}
        pool._browser_health[mock_browser2] = {"usage_count": 0}
        pool._browser_health[mock_browser3] = {"usage_count": 0}

        # Put 2 browsers in available queue
        pool._available_browsers.put_nowait(mock_browser1)
        pool._available_browsers.put_nowait(mock_browser2)

        stats = pool.get_pool_stats()

        assert stats["total_browsers"] == 3
        assert stats["available_browsers"] == 2
        assert stats["busy_browsers"] == 1

    def test_get_pool_stats_usage_total(self):
        """Test get_pool_stats returns correct usage total."""
        pool = BrowserPool()

        mock_browser1 = Mock()
        mock_browser2 = Mock()
        pool._all_browsers.add(mock_browser1)
        pool._all_browsers.add(mock_browser2)
        pool._browser_health[mock_browser1] = {"usage_count": 3}
        pool._browser_health[mock_browser2] = {"usage_count": 2}

        stats = pool.get_pool_stats()
        assert stats["total_usage"] == 5

    def test_get_pool_stats_efficiency(self):
        """Test get_pool_stats calculates efficiency correctly."""
        pool = BrowserPool()

        mock_browser1 = Mock()
        mock_browser2 = Mock()
        pool._all_browsers.add(mock_browser1)
        pool._all_browsers.add(mock_browser2)
        pool._browser_health[mock_browser1] = {"usage_count": 2}
        pool._browser_health[mock_browser2] = {"usage_count": 2}

        stats = pool.get_pool_stats()
        # 4 total usage / 2 browsers = 2.0 efficiency
        assert stats["pool_efficiency"] == 2.0

    def test_get_pool_stats_empty_pool(self):
        """Test get_pool_stats with empty pool."""
        pool = BrowserPool()
        stats = pool.get_pool_stats()

        assert stats["total_browsers"] == 0
        assert stats["available_browsers"] == 0
        assert stats["busy_browsers"] == 0
        assert stats["total_usage"] == 0
        assert stats["pool_efficiency"] == 0

    def test_get_pool_stats_memory_limit(self):
        """Test get_pool_stats includes memory limit."""
        config = BrowserConfig(memory_limit_mb=256)
        pool = BrowserPool(config)

        stats = pool.get_pool_stats()

        assert stats["memory_limit_mb"] == 256


class TestBrowserPoolContext:
    """Test BrowserPool.create_context()."""

    @pytest.mark.asyncio
    async def test_create_context_default_settings(self):
        """Test create_context uses default settings."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        await pool.create_context(mock_browser)

        mock_browser.new_context.assert_called_once()
        call_kwargs = mock_browser.new_context.call_args[1]
        assert "Mozilla" in call_kwargs["user_agent"]
        assert call_kwargs["viewport"] == {"width": 1280, "height": 720}

    @pytest.mark.asyncio
    async def test_create_context_custom_user_agent(self):
        """Test create_context uses custom user agent."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        custom_ua = "CustomBot/1.0"
        await pool.create_context(mock_browser, user_agent=custom_ua)

        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["user_agent"] == custom_ua

    @pytest.mark.asyncio
    async def test_create_context_custom_viewport(self):
        """Test create_context uses custom viewport."""
        pool = BrowserPool()

        mock_browser = Mock()
        mock_context = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        custom_viewport = {"width": 1920, "height": 1080}
        await pool.create_context(mock_browser, viewport=custom_viewport)

        call_kwargs = mock_browser.new_context.call_args[1]
        assert call_kwargs["viewport"] == custom_viewport

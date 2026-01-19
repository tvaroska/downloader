"""Tests for browser pool manager."""

from unittest.mock import Mock

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

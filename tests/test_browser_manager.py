"""Tests for browser pool manager."""

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

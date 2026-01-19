"""Integration tests for browser security measures."""

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.downloader.browser.manager import BrowserConfig, BrowserPool
from src.downloader.validation import URLValidationError, validate_url


class TestFileURLBlocking:
    """Test that file:// URLs are blocked for security."""

    def test_file_url_blocked_etc_passwd(self):
        """Test that file:///etc/passwd is blocked."""
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("file:///etc/passwd")

        assert "file://" in str(exc_info.value).lower()
        assert "not allowed" in str(exc_info.value).lower()

    def test_file_url_blocked_relative(self):
        """Test that relative file:// URLs are blocked."""
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("file://./secret.txt")

        assert "file://" in str(exc_info.value).lower()

    def test_file_url_blocked_home_ssh(self):
        """Test that file:///home/user/.ssh/id_rsa is blocked."""
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("file:///home/user/.ssh/id_rsa")

        assert "file://" in str(exc_info.value).lower()

    def test_file_url_blocked_windows(self):
        """Test that Windows-style file:// URLs are blocked."""
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("file:///C:/Windows/System32/config/SAM")

        assert "file://" in str(exc_info.value).lower()

    def test_file_url_error_message_user_friendly(self):
        """Test that error message is clear and user-friendly."""
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("file:///etc/passwd")

        error_msg = str(exc_info.value)
        # Should mention local file system access is not allowed
        assert "local file system" in error_msg.lower() or "file://" in error_msg.lower()
        assert "not allowed" in error_msg.lower()

    def test_http_url_allowed(self):
        """Test that http:// URLs pass validation."""
        # This should not raise - just validate the scheme is accepted
        # Note: may raise other validation errors (SSRF, etc) but not scheme error
        try:
            result = validate_url("http://example.com")
            assert result.startswith("http://")
        except URLValidationError as e:
            # If it raises, it should NOT be about scheme
            assert "file://" not in str(e).lower()
            assert "http" not in str(e).lower() or "https" in str(e).lower()

    def test_https_url_allowed(self):
        """Test that https:// URLs pass validation."""
        try:
            result = validate_url("https://example.com")
            assert result.startswith("https://")
        except URLValidationError as e:
            # If it raises, it should NOT be about scheme
            assert "file://" not in str(e).lower()


class TestMemoryLimitEnforcement:
    """Test browser memory limit configuration."""

    def test_default_memory_limit_512mb(self):
        """Test that default config has 512MB memory limit."""
        config = BrowserConfig()
        assert config.memory_limit_mb == 512

    def test_custom_memory_limit_256mb(self):
        """Test that custom memory limit is respected."""
        config = BrowserConfig(memory_limit_mb=256)
        assert config.memory_limit_mb == 256

    def test_custom_memory_limit_1024mb(self):
        """Test larger memory limit configuration."""
        config = BrowserConfig(memory_limit_mb=1024)
        assert config.memory_limit_mb == 1024

    def test_memory_flag_in_launch_args(self):
        """Test that --js-flags contains memory limit."""
        config = BrowserConfig(memory_limit_mb=512)
        args = config.get_launch_args()

        # Find the js-flags argument
        js_flags_arg = None
        for arg in args:
            if "--js-flags=" in arg:
                js_flags_arg = arg
                break

        assert js_flags_arg is not None
        assert "--js-flags=--max-old-space-size=512" in js_flags_arg

    def test_memory_flag_custom_value_in_launch_args(self):
        """Test custom memory limit appears correctly in launch args."""
        config = BrowserConfig(memory_limit_mb=768)
        args = config.get_launch_args()

        assert "--js-flags=--max-old-space-size=768" in args

    def test_webgl_disabled_in_launch_args(self):
        """Test that --disable-webgl is in launch args for security."""
        config = BrowserConfig()
        args = config.get_launch_args()

        assert "--disable-webgl" in args

    def test_security_flags_present(self):
        """Test that essential security flags are present."""
        config = BrowserConfig()
        args = config.get_launch_args()

        # These flags should be present for security
        assert "--no-sandbox" in args
        assert "--disable-extensions" in args
        assert "--disable-plugins" in args
        assert "--disable-remote-fonts" in args


@pytest.mark.asyncio
class TestProcessCleanup:
    """Test zombie process cleanup after timeout."""

    async def test_force_kill_sends_sigterm_first(self):
        """Test that SIGTERM is sent first before SIGKILL."""
        pool = BrowserPool()
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch("src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock):
                # Process terminates after SIGTERM
                mock_kill.side_effect = [None, ProcessLookupError()]

                await pool._force_kill_process(test_pid)

                # First call should be SIGTERM
                first_call = mock_kill.call_args_list[0]
                assert first_call == call(test_pid, signal.SIGTERM)

    async def test_force_kill_waits_before_sigkill(self):
        """Test that pool waits force_kill_timeout before SIGKILL."""
        config = BrowserConfig(force_kill_timeout=2.0)
        pool = BrowserPool(config)
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch(
                "src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock
            ) as mock_sleep:
                # Process terminates after SIGTERM
                mock_kill.side_effect = [None, ProcessLookupError()]

                await pool._force_kill_process(test_pid)

                mock_sleep.assert_called_once_with(2.0)

    async def test_force_kill_sends_sigkill_if_process_alive(self):
        """Test that SIGKILL is sent if process survives SIGTERM."""
        pool = BrowserPool()
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch("src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock):
                # Process stays alive: SIGTERM, check (0), then SIGKILL
                mock_kill.side_effect = [None, None, None]

                await pool._force_kill_process(test_pid)

                calls = mock_kill.call_args_list
                assert len(calls) >= 3
                # First: SIGTERM
                assert calls[0] == call(test_pid, signal.SIGTERM)
                # Second: check if alive (signal 0)
                assert calls[1] == call(test_pid, 0)
                # Third: SIGKILL
                assert calls[2] == call(test_pid, signal.SIGKILL)

    async def test_force_kill_skips_sigkill_if_process_terminated(self):
        """Test that SIGKILL is skipped if process already terminated."""
        pool = BrowserPool()
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch("src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock):
                # Process terminates: SIGTERM succeeds, check raises ProcessLookupError
                mock_kill.side_effect = [None, ProcessLookupError()]

                await pool._force_kill_process(test_pid)

                calls = mock_kill.call_args_list
                # Should only have SIGTERM and check calls, no SIGKILL
                assert len(calls) == 2
                assert calls[0] == call(test_pid, signal.SIGTERM)
                assert calls[1] == call(test_pid, 0)

    async def test_force_kill_handles_permission_error(self):
        """Test graceful handling when permission to kill is denied."""
        pool = BrowserPool()
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch("src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock):
                mock_kill.side_effect = PermissionError("Operation not permitted")

                # Should not raise exception
                await pool._force_kill_process(test_pid)
                # Verify kill was attempted
                mock_kill.assert_called()

    async def test_force_kill_handles_process_not_found_on_sigterm(self):
        """Test handling when process not found during SIGTERM."""
        pool = BrowserPool()
        test_pid = 12345

        with patch("src.downloader.browser.manager.os.kill") as mock_kill:
            with patch("src.downloader.browser.manager.asyncio.sleep", new_callable=AsyncMock):
                mock_kill.side_effect = ProcessLookupError("No such process")

                # Should not raise exception
                await pool._force_kill_process(test_pid)

    async def test_close_browser_graceful_success(self):
        """Test that browser closes gracefully within timeout."""
        config = BrowserConfig(close_timeout=5.0)
        pool = BrowserPool(config)

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_browser.close = AsyncMock()

        result = await pool._close_browser_with_timeout(mock_browser, pid=12345)

        assert result is True
        mock_browser.close.assert_called_once()

    async def test_close_browser_timeout_triggers_force_kill(self):
        """Test that timeout during close triggers force kill."""
        config = BrowserConfig(close_timeout=0.1, force_kill_timeout=0.1)
        pool = BrowserPool(config)

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True

        # Simulate timeout during close
        async def slow_close():
            await asyncio.sleep(10)  # Will timeout

        mock_browser.close = slow_close

        with patch.object(pool, "_force_kill_process", new_callable=AsyncMock) as mock_force_kill:
            result = await pool._close_browser_with_timeout(mock_browser, pid=12345)

            assert result is False
            mock_force_kill.assert_called_once_with(12345)

    async def test_close_browser_uses_configured_timeout(self):
        """Test that close uses configured timeout value."""
        config = BrowserConfig(close_timeout=10.0)
        pool = BrowserPool(config)

        mock_browser = MagicMock()
        mock_browser.is_connected.return_value = True
        mock_browser.close = AsyncMock()

        with patch(
            "src.downloader.browser.manager.asyncio.wait_for", new_callable=AsyncMock
        ) as mock_wait_for:
            mock_wait_for.return_value = None

            await pool._close_browser_with_timeout(mock_browser, pid=12345)

            mock_wait_for.assert_called_once()
            call_args = mock_wait_for.call_args
            assert call_args[1]["timeout"] == 10.0

    async def test_pool_default_config_values(self):
        """Test BrowserPool has correct default config."""
        pool = BrowserPool()

        assert pool.config.pool_size == 3
        assert pool.config.memory_limit_mb == 512
        assert pool.config.acquire_timeout == 30.0
        assert pool.config.headless is True
        assert pool.config.close_timeout == 5.0
        assert pool.config.force_kill_timeout == 2.0

    async def test_pool_custom_config(self):
        """Test BrowserPool accepts custom config."""
        config = BrowserConfig(
            pool_size=5,
            memory_limit_mb=1024,
            close_timeout=10.0,
            force_kill_timeout=5.0,
        )
        pool = BrowserPool(config)

        assert pool.config.pool_size == 5
        assert pool.config.memory_limit_mb == 1024
        assert pool.config.close_timeout == 10.0
        assert pool.config.force_kill_timeout == 5.0

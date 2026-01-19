"""Browser pool management for Playwright with memory limits."""

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Browser, BrowserContext, async_playwright

logger = logging.getLogger(__name__)


class BrowserPoolError(Exception):
    """Raised when browser pool operations fail."""

    pass


@dataclass
class BrowserConfig:
    """Configuration for browser pool behavior.

    Attributes:
        pool_size: Number of browser instances to maintain
        memory_limit_mb: JavaScript heap memory limit per browser (MB)
        acquire_timeout: Timeout in seconds for acquiring a browser
        headless: Run browsers in headless mode
        close_timeout: Timeout in seconds for graceful browser close
        force_kill_timeout: Seconds to wait before SIGKILL after SIGTERM
    """

    pool_size: int = 3
    memory_limit_mb: int = 512
    acquire_timeout: float = 30.0
    headless: bool = True
    close_timeout: float = 5.0
    force_kill_timeout: float = 2.0

    # Standard browser launch arguments
    launch_args: list[str] = field(
        default_factory=lambda: [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-features=VizDisplayCompositor",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-field-trial-config",
            "--disable-ipc-flooding-protection",
            "--disable-checker-imaging",
            "--disable-extensions",
            "--disable-plugins",
            "--disable-default-apps",
            "--disable-remote-fonts",
            "--disable-webgl",
        ]
    )

    def get_launch_args(self) -> list[str]:
        """Get launch args including memory limit flag."""
        args = self.launch_args.copy()
        # Add memory limit via V8 JavaScript engine flag
        args.append(f"--js-flags=--max-old-space-size={self.memory_limit_mb}")
        return args


class BrowserPool:
    """Optimized pool of browser instances with memory limits.

    Uses queue-based approach for O(1) browser selection instead of O(n) linear search.
    Implements automatic health monitoring and dynamic pool management.
    Enforces memory limits per browser via Chromium flags.
    """

    def __init__(self, config: BrowserConfig | None = None):
        self.config = config or BrowserConfig()
        self._available_browsers: asyncio.Queue[Browser] = asyncio.Queue(
            maxsize=self.config.pool_size
        )
        self._all_browsers: set[Browser] = set()
        self._browser_health: dict[Browser, dict[str, Any]] = {}
        self._playwright = None
        self._lock = asyncio.Lock()
        self._closed = False

    @property
    def pool_size(self) -> int:
        """Get pool size for backward compatibility."""
        return self.config.pool_size

    async def start(self):
        """Initialize the browser pool with queue-based management."""
        try:
            self._playwright = await async_playwright().start()
            self._closed = False

            # Initialize browsers and add them to the available queue
            for i in range(self.config.pool_size):
                browser = await self._launch_browser()
                self._all_browsers.add(browser)
                self._browser_health[browser] = {
                    "usage_count": 0,
                    "last_used": asyncio.get_event_loop().time(),
                    "healthy": True,
                    "pid": self._get_browser_pid(browser),
                }
                await self._available_browsers.put(browser)
                logger.info(f"Browser {i + 1}/{self.config.pool_size} initialized")

            logger.info(
                f"Browser pool initialized with {self.config.pool_size} browsers "
                f"(memory limit: {self.config.memory_limit_mb}MB)"
            )
        except Exception as e:
            logger.error(f"Failed to initialize browser pool: {e}")
            raise BrowserPoolError(f"Browser pool initialization failed: {e}")

    async def _launch_browser(self) -> Browser:
        """Launch a single browser instance with memory limits."""
        return await self._playwright.chromium.launch(
            headless=self.config.headless,
            args=self.config.get_launch_args(),
        )

    def _get_browser_pid(self, browser: Browser) -> int | None:
        """Extract the process ID from a Playwright browser instance.

        Accesses Playwright internals to get the Chromium process PID.
        Returns None if PID cannot be extracted.
        """
        try:
            impl = getattr(browser, "_impl_obj", None)
            if impl:
                process = getattr(impl, "_browser_process", None)
                if process:
                    return process.pid
            return None
        except Exception:
            return None

    async def _force_kill_process(self, pid: int) -> None:
        """Force kill a browser process that failed to close gracefully.

        Sends SIGTERM first, waits briefly, then sends SIGKILL if needed.
        """
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info(f"Sent SIGTERM to browser process (pid: {pid})")

            await asyncio.sleep(self.config.force_kill_timeout)

            try:
                os.kill(pid, 0)  # Check if process still exists
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Force-killed browser process with SIGKILL (pid: {pid})")
            except ProcessLookupError:
                logger.debug(f"Process {pid} terminated after SIGTERM")
        except ProcessLookupError:
            logger.debug(f"Process {pid} not found (already terminated)")
        except PermissionError:
            logger.error(f"Permission denied killing process {pid}")
        except Exception as e:
            logger.error(f"Failed to kill process {pid}: {e}")

    async def _close_browser_with_timeout(self, browser: Browser, pid: int | None = None) -> bool:
        """Close a browser gracefully with timeout, force-kill if needed.

        Args:
            browser: The browser instance to close
            pid: Optional PID (use if already extracted before health tracking removal)

        Returns:
            True if closed gracefully, False if force-killed or failed
        """
        if pid is None:
            pid = self._browser_health.get(browser, {}).get("pid")

        try:
            if browser.is_connected():
                await asyncio.wait_for(browser.close(), timeout=self.config.close_timeout)
                logger.debug(f"Browser closed gracefully (pid: {pid})")
                return True
        except asyncio.TimeoutError:
            logger.warning(
                f"Browser close timed out after {self.config.close_timeout}s (pid: {pid})"
            )
        except Exception as e:
            logger.warning(f"Browser close failed: {e} (pid: {pid})")

        if pid:
            await self._force_kill_process(pid)
            return False

        return False

    async def get_browser(self) -> Browser:
        """Get an available browser from the pool with O(1) selection."""
        if self._closed:
            raise BrowserPoolError("Browser pool is closed")

        try:
            # O(1) browser selection using queue
            browser = await asyncio.wait_for(
                self._available_browsers.get(), timeout=self.config.acquire_timeout
            )

            # Update health metrics
            if browser in self._browser_health:
                self._browser_health[browser]["usage_count"] += 1
                self._browser_health[browser]["last_used"] = asyncio.get_event_loop().time()

            return browser
        except asyncio.TimeoutError:
            raise BrowserPoolError(
                f"No browser available within timeout ({self.config.acquire_timeout}s)"
            )
        except Exception as e:
            raise BrowserPoolError(f"Failed to get browser from pool: {e}")

    async def release_browser(self, browser: Browser):
        """Release a browser back to the pool with health check."""
        if self._closed or browser not in self._all_browsers:
            return

        try:
            # Health check: ensure browser is still functional
            if await self._is_browser_healthy(browser):
                # Return healthy browser to available queue
                await self._available_browsers.put(browser)
            else:
                # Replace unhealthy browser
                logger.warning("Replacing unhealthy browser in pool")
                await self._replace_browser(browser)
        except Exception as e:
            logger.error(f"Error releasing browser: {e}")
            # Try to replace the problematic browser
            await self._replace_browser(browser)

    async def _is_browser_healthy(self, browser: Browser) -> bool:
        """Check if a browser instance is still healthy."""
        try:
            # Simple health check: verify browser is connected
            return browser.is_connected()
        except Exception:
            return False

    async def _replace_browser(self, old_browser: Browser):
        """Replace an unhealthy browser with a new one."""
        try:
            # Get PID before removing from tracking
            pid = self._browser_health.get(old_browser, {}).get("pid")

            # Remove old browser from tracking
            self._all_browsers.discard(old_browser)
            self._browser_health.pop(old_browser, None)

            # Close old browser with timeout, force-kill if necessary
            await self._close_browser_with_timeout(old_browser, pid)

            # Create new browser
            new_browser = await self._launch_browser()
            self._all_browsers.add(new_browser)
            self._browser_health[new_browser] = {
                "usage_count": 0,
                "last_used": asyncio.get_event_loop().time(),
                "healthy": True,
                "pid": self._get_browser_pid(new_browser),
            }

            # Add to available queue
            await self._available_browsers.put(new_browser)
            logger.info("Successfully replaced unhealthy browser")
        except Exception as e:
            logger.error(f"Failed to replace browser: {e}")

    async def close(self):
        """Close all browsers and cleanup with timeout."""
        self._closed = True

        try:
            # Close all browsers with timeout
            close_tasks = [
                self._close_browser_with_timeout(browser)
                for browser in self._all_browsers
                if browser
            ]

            if close_tasks:
                # Overall timeout for entire pool shutdown
                pool_timeout = self.config.close_timeout * len(close_tasks) + 5.0
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*close_tasks, return_exceptions=True),
                        timeout=pool_timeout,
                    )
                except asyncio.TimeoutError:
                    logger.error("Pool close timed out, some browsers may be orphaned")

            # Clear all data structures
            self._all_browsers.clear()
            self._browser_health.clear()

            # Clear the queue
            while not self._available_browsers.empty():
                try:
                    self._available_browsers.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

            logger.info("Browser pool closed")
        except Exception as e:
            logger.error(f"Error closing browser pool: {e}")

    def get_pool_stats(self) -> dict:
        """Get browser pool statistics for monitoring."""
        available_count = self._available_browsers.qsize()
        total_usage = sum(stats["usage_count"] for stats in self._browser_health.values())

        return {
            "total_browsers": len(self._all_browsers),
            "available_browsers": available_count,
            "busy_browsers": len(self._all_browsers) - available_count,
            "total_usage": total_usage,
            "pool_efficiency": (total_usage / max(len(self._all_browsers), 1))
            if self._all_browsers
            else 0,
            "memory_limit_mb": self.config.memory_limit_mb,
        }

    async def create_context(
        self,
        browser: Browser,
        user_agent: str | None = None,
        viewport: dict[str, int] | None = None,
    ) -> BrowserContext:
        """Create an isolated browser context with standard settings.

        Args:
            browser: Browser instance from the pool
            user_agent: Optional custom user agent
            viewport: Optional viewport dimensions

        Returns:
            Isolated browser context
        """
        return await browser.new_context(
            user_agent=user_agent
            or (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport=viewport or {"width": 1280, "height": 720},
            ignore_https_errors=False,
            java_script_enabled=True,
            bypass_csp=False,
        )

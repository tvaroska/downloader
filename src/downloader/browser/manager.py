"""Browser pool management for Playwright with memory limits and hardened contexts."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Playwright,
    ViewportSize,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from ..validation import EgressPolicy
from .egress_proxy import BrowserEgressProxy

logger = logging.getLogger(__name__)


class BrowserPoolError(Exception):
    """Raised when browser pool operations fail."""


@dataclass
class BrowserConfig:
    """Configuration for browser pool behavior."""

    pool_size: int = 3
    memory_limit_mb: int = 512
    acquire_timeout: float = 30.0
    headless: bool = True
    close_timeout: float = 5.0
    force_kill_timeout: float = 2.0
    max_uses_per_browser: int = 50

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
        """Get launch args including V8 heap memory limit flag."""
        args = self.launch_args.copy()
        args.append(f"--js-flags=--max-old-space-size={self.memory_limit_mb}")
        return args


class BrowserPool:
    """Queue-backed pool of Playwright Chromium instances with connect-time SSRF proxy."""

    def __init__(
        self,
        config: BrowserConfig | None = None,
        *,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self.config = config or BrowserConfig()
        self._egress_policy = egress_policy
        self._available_browsers: asyncio.Queue[Browser] = asyncio.Queue(
            maxsize=self.config.pool_size
        )
        self._all_browsers: set[Browser] = set()
        self._browser_health: dict[Browser, dict[str, Any]] = {}
        self._playwright: Playwright | None = None
        self._egress_proxy: BrowserEgressProxy | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._recycled_count: int = 0

    @property
    def pool_size(self) -> int:
        """Return configured browser pool size."""
        return self.config.pool_size

    async def start(self) -> None:
        """Initialize the browser pool and loopback SSRF egress proxy."""
        try:
            proxy = BrowserEgressProxy(egress_policy=self._egress_policy)
            await proxy.start()
            self._egress_proxy = proxy
            self._playwright = await async_playwright().start()
            self._closed = False

            for i in range(self.config.pool_size):
                browser = await self._launch_browser()
                self._all_browsers.add(browser)
                self._browser_health[browser] = {
                    "usage_count": 0,
                    "last_used": asyncio.get_running_loop().time(),
                    "healthy": True,
                    "pid": self._get_browser_pid(browser),
                }
                await self._available_browsers.put(browser)
                logger.info(f"Browser {i + 1}/{self.config.pool_size} initialized")

            logger.info(
                f"Browser pool initialized with {self.config.pool_size} browsers "
                f"(memory limit: {self.config.memory_limit_mb}MB)"
            )
        except Exception as exc:  # noqa: BLE001
            # Roll back partially started proxy/driver on any Chromium or mock startup error.
            if self._playwright is not None:
                try:
                    await self._playwright.stop()
                except (PlaywrightError, OSError, RuntimeError):
                    pass
                self._playwright = None
            if self._egress_proxy is not None:
                await self._egress_proxy.close()
                self._egress_proxy = None
            logger.error(f"Failed to initialize browser pool: {exc}")
            raise BrowserPoolError(f"Browser pool initialization failed: {exc}") from exc

    async def _launch_browser(self) -> Browser:
        """Launch a single browser instance with memory limits and connect-time SSRF proxy."""
        if self._playwright is None:
            raise BrowserPoolError("Playwright instance is not initialized")
        launch_kwargs: dict[str, Any] = {
            "headless": self.config.headless,
            "args": self.config.get_launch_args(),
        }
        if self._egress_proxy is not None and self._egress_proxy.port > 0:
            launch_kwargs["proxy"] = {
                "server": self._egress_proxy.server_url,
                "bypass": "<-loopback>",
            }
        return await self._playwright.chromium.launch(**launch_kwargs)

    def _get_browser_pid(self, browser: Browser) -> int | None:
        """Extract the process ID from a Playwright browser instance when exposed."""
        try:
            impl = getattr(browser, "_impl_obj", None)
            if impl:
                process = getattr(impl, "_browser_process", None)
                if process:
                    pid = getattr(process, "pid", None)
                    return pid if isinstance(pid, int) and not isinstance(pid, bool) else None
            return None
        except (AttributeError, TypeError):
            return None

    async def _force_kill_process(self, pid: int) -> None:
        """Send SIGTERM then SIGKILL to an unresponsive browser process."""
        try:
            os.kill(pid, signal.SIGTERM)
            logger.info(f"Sent SIGTERM to browser process (pid: {pid})")
            await asyncio.sleep(self.config.force_kill_timeout)
            try:
                os.kill(pid, 0)
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Force-killed browser process with SIGKILL (pid: {pid})")
            except ProcessLookupError:
                logger.debug(f"Process {pid} terminated after SIGTERM")
        except ProcessLookupError:
            logger.debug(f"Process {pid} not found (already terminated)")
        except PermissionError:
            logger.error(f"Permission denied killing process {pid}")
        except OSError as exc:
            logger.error(f"Failed to kill process {pid}: {exc}")

    async def _close_browser_with_timeout(self, browser: Browser, pid: int | None = None) -> bool:
        """Close a browser gracefully with timeout, falling back to force-kill when PID is known."""
        if pid is None:
            pid = self._browser_health.get(browser, {}).get("pid")

        try:
            if browser.is_connected():
                await asyncio.wait_for(browser.close(), timeout=self.config.close_timeout)
                logger.debug(f"Browser closed gracefully (pid: {pid})")
                return True
        except TimeoutError:
            logger.warning(
                f"Browser close timed out after {self.config.close_timeout}s (pid: {pid})"
            )
        except Exception as exc:  # noqa: BLE001
            # Disconnected Playwright transports or test doubles can raise arbitrary close errors.
            logger.warning(f"Browser close failed: {exc} (pid: {pid})")

        if pid:
            await self._force_kill_process(pid)
            return False

        return False

    async def get_browser(self) -> Browser:
        """Acquire an available browser from the pool within `acquire_timeout`."""
        if self._closed:
            raise BrowserPoolError("Browser pool is closed")

        try:
            browser = await asyncio.wait_for(
                self._available_browsers.get(), timeout=self.config.acquire_timeout
            )
            if browser in self._browser_health:
                self._browser_health[browser]["usage_count"] += 1
                self._browser_health[browser]["last_used"] = asyncio.get_running_loop().time()
            return browser
        except TimeoutError as exc:
            raise BrowserPoolError(
                f"No browser available within timeout ({self.config.acquire_timeout}s)"
            ) from exc
        except (PlaywrightError, RuntimeError, OSError) as exc:
            raise BrowserPoolError(f"Failed to get browser from pool: {exc}") from exc

    async def release_browser(self, browser: Browser) -> None:
        """Release a browser back to the pool with health check and max-use recycling."""
        if self._closed or browser not in self._all_browsers:
            return

        try:
            if await self._is_browser_healthy(browser):
                usage_count = self._browser_health.get(browser, {}).get("usage_count", 0)
                if (
                    self.config.max_uses_per_browser > 0
                    and usage_count >= self.config.max_uses_per_browser
                ):
                    logger.info(
                        f"Recycling browser after {usage_count} uses "
                        f"(max_uses_per_browser={self.config.max_uses_per_browser})"
                    )
                    self._recycled_count += 1
                    await self._replace_browser(browser)
                else:
                    await self._available_browsers.put(browser)
            else:
                logger.warning("Replacing unhealthy browser in pool")
                await self._replace_browser(browser)
        except (PlaywrightError, RuntimeError, OSError) as exc:
            logger.error(f"Error releasing browser: {exc}")
            await self._replace_browser(browser)

    async def _is_browser_healthy(self, browser: Browser) -> bool:
        """Check whether a browser instance remains connected."""
        try:
            return bool(browser.is_connected())
        except Exception:  # noqa: BLE001
            # Treat any broken transport/mock inspection failure as an unhealthy browser.
            return False

    async def _replace_browser(self, old_browser: Browser) -> None:
        """Close `old_browser` and launch a replacement instance."""
        try:
            pid = self._browser_health.get(old_browser, {}).get("pid")
            self._all_browsers.discard(old_browser)
            self._browser_health.pop(old_browser, None)
            await self._close_browser_with_timeout(old_browser, pid)

            if self._closed:
                return

            new_browser = await self._launch_browser()
            if self._closed:
                await self._close_browser_with_timeout(
                    new_browser, self._get_browser_pid(new_browser)
                )
                return

            self._all_browsers.add(new_browser)
            self._browser_health[new_browser] = {
                "usage_count": 0,
                "last_used": asyncio.get_running_loop().time(),
                "healthy": True,
                "pid": self._get_browser_pid(new_browser),
            }
            await self._available_browsers.put(new_browser)
            logger.info("Successfully replaced unhealthy browser")
        except Exception as exc:  # noqa: BLE001
            # Never propagate replacement launch failures out of finally/release cleanup.
            logger.error(f"Failed to replace browser: {exc}")

    async def close(self) -> None:
        """Close all browsers, stop Playwright, and shut down the loopback proxy."""
        self._closed = True

        try:
            close_tasks = [
                self._close_browser_with_timeout(browser)
                for browser in self._all_browsers
                if browser
            ]
            if close_tasks:
                pool_timeout = self.config.close_timeout * len(close_tasks) + 5.0
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*close_tasks, return_exceptions=True),
                        timeout=pool_timeout,
                    )
                except TimeoutError:
                    logger.error("Pool close timed out, some browsers may be orphaned")

            self._all_browsers.clear()
            self._browser_health.clear()

            while not self._available_browsers.empty():
                try:
                    self._available_browsers.get_nowait()
                except asyncio.QueueEmpty:
                    break

            if self._playwright:
                await self._playwright.stop()
                self._playwright = None

            if self._egress_proxy is not None:
                await self._egress_proxy.close()
                self._egress_proxy = None

            logger.info("Browser pool closed")
        except (PlaywrightError, RuntimeError, OSError) as exc:
            logger.error(f"Error closing browser pool: {exc}")

    async def create_context(
        self,
        browser: Browser,
        user_agent: str | None = None,
        viewport: ViewportSize | None = None,
    ) -> BrowserContext:
        """Create an isolated browser context with downloads and service workers disabled."""
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
            service_workers="block",
            accept_downloads=False,
        )

"""Browser pool management for Playwright with memory limits and hardened contexts."""

from __future__ import annotations

import asyncio
import logging
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
                }
                await self._available_browsers.put(browser)
                logger.info(f"Browser {i + 1}/{self.config.pool_size} initialized")

            logger.info(
                f"Browser pool initialized with {self.config.pool_size} browsers "
                f"(memory limit: {self.config.memory_limit_mb}MB)"
            )
        except Exception as exc:  # noqa: BLE001
            # Roll back partially started browsers, driver, and proxy on any startup error.
            for started_browser in list(self._all_browsers):
                await self._close_browser_with_timeout(started_browser)
            self._all_browsers.clear()
            self._browser_health.clear()
            while not self._available_browsers.empty():
                try:
                    self._available_browsers.get_nowait()
                except asyncio.QueueEmpty:
                    break
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

    async def _close_browser_with_timeout(self, browser: Browser) -> bool:
        """Close a browser gracefully within `close_timeout`."""
        try:
            if browser.is_connected():
                await asyncio.wait_for(browser.close(), timeout=self.config.close_timeout)
                logger.debug("Browser closed gracefully")
                return True
        except TimeoutError:
            logger.warning(f"Browser close timed out after {self.config.close_timeout}s")
        except Exception as exc:  # noqa: BLE001
            # Disconnected Playwright transports or test doubles can raise arbitrary close errors.
            logger.warning(f"Browser close failed: {exc}")

        return False

    async def _replenish_browser_if_needed(self) -> Browser | None:
        """Launch a fresh browser when tracked pool capacity has dropped below `pool_size`."""
        async with self._lock:
            if self._closed or len(self._all_browsers) >= self.config.pool_size:
                return None
            new_browser = await self._launch_browser()
            if self._closed:
                await self._close_browser_with_timeout(new_browser)
                return None
            self._all_browsers.add(new_browser)
            self._browser_health[new_browser] = {
                "usage_count": 1,
                "last_used": asyncio.get_running_loop().time(),
                "healthy": True,
            }
            logger.info("Replenished browser pool after prior replacement failure")
            return new_browser

    async def get_browser(self) -> Browser:
        """Acquire an available browser from the pool within `acquire_timeout`."""
        if self._closed:
            raise BrowserPoolError("Browser pool is closed")

        try:
            try:
                browser = self._available_browsers.get_nowait()
            except asyncio.QueueEmpty:
                if len(self._all_browsers) < self.config.pool_size:
                    replenished = await self._replenish_browser_if_needed()
                    if replenished is not None:
                        return replenished
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
                    await asyncio.shield(self._replace_browser(browser))
                else:
                    await self._available_browsers.put(browser)
            else:
                logger.warning("Replacing unhealthy browser in pool")
                await asyncio.shield(self._replace_browser(browser))
        except (PlaywrightError, RuntimeError, OSError) as exc:
            logger.error(f"Error releasing browser: {exc}")
            await asyncio.shield(self._replace_browser(browser))

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
            self._all_browsers.discard(old_browser)
            self._browser_health.pop(old_browser, None)
            await self._close_browser_with_timeout(old_browser)

            if self._closed:
                return

            new_browser = await self._launch_browser()
            if self._closed:
                await self._close_browser_with_timeout(new_browser)
                return

            self._all_browsers.add(new_browser)
            self._browser_health[new_browser] = {
                "usage_count": 0,
                "last_used": asyncio.get_running_loop().time(),
                "healthy": True,
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

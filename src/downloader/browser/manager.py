"""Browser pool management for Playwright with memory limits and hardened contexts."""

from __future__ import annotations

import asyncio
import contextlib
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

from ..ports import (
    BrowserAcquireTimeoutError as BrowserAcquireTimeoutError,
)
from ..ports import (
    BrowserPoolError as BrowserPoolError,
)
from ..validation import EgressPolicy
from .egress_proxy import BrowserEgressProxy

logger = logging.getLogger(__name__)


def _extract_browser_process(browser: Any) -> Any:
    for owner in (browser, getattr(browser, "_impl_obj", None)):
        if owner is None:
            continue
        for attr in ("_browser_process", "_process", "process"):
            proc = getattr(owner, attr, None)
            if proc is not None and not callable(proc):
                return proc
    return None


def _extract_browser_pid(browser: Any) -> int | None:
    proc = _extract_browser_process(browser)
    raw_pid = getattr(proc, "pid", None) if proc is not None else getattr(browser, "pid", None)
    return raw_pid if isinstance(raw_pid, int) and raw_pid > 0 else None


def _kill_browser_process(browser: Any, fallback_pid: int | None = None) -> bool:
    proc = _extract_browser_process(browser)
    kill_fn = getattr(proc, "kill", None) if proc is not None else None
    if callable(kill_fn):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError, RuntimeError):
            kill_fn()
            return True
    pid = _extract_browser_pid(browser) or fallback_pid
    if pid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, signal.SIGKILL)
            return True
    return False


@dataclass
class BrowserConfig:
    """Configuration for browser pool behavior."""

    pool_size: int = 3
    memory_limit_mb: int = 512
    acquire_timeout: float = 30.0
    headless: bool = True
    close_timeout: float = 5.0
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
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--enforce-webrtc-ip-permission-check",
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

    @property
    def available_count(self) -> int:
        """Return the number of idle browsers currently available in the pool."""
        return 0 if self._closed else max(0, self._available_browsers.qsize())

    @property
    def active_browser_count(self) -> int:
        """Return total tracked browser instances currently managed by this pool."""
        return len(self._all_browsers)

    @property
    def is_closed(self) -> bool:
        """Return True once the pool has been closed."""
        return self._closed

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
                    "pid": _extract_browser_pid(browser),
                }
                await self._available_browsers.put(browser)
                logger.info(f"Browser {i + 1}/{self.config.pool_size} initialized")

            logger.info(
                f"Browser pool initialized with {self.config.pool_size} browsers "
                f"(memory limit: {self.config.memory_limit_mb}MB)"
            )
        except (PlaywrightError, BrowserPoolError, OSError, RuntimeError, ValueError) as exc:
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
        """Close a browser gracefully within `close_timeout`, force-killing hung processes on timeout."""
        tracked_pid = self._browser_health.get(browser, {}).get("pid")
        pid = tracked_pid if isinstance(tracked_pid, int) else _extract_browser_pid(browser)
        try:
            if browser.is_connected():
                await asyncio.wait_for(browser.close(), timeout=self.config.close_timeout)
                logger.debug("Browser closed gracefully")
                return True
        except TimeoutError:
            killed = _kill_browser_process(browser, pid)
            logger.warning(
                f"Browser close timed out after {self.config.close_timeout}s (force_killed={killed})"
            )
        except (PlaywrightError, BrowserPoolError, OSError, RuntimeError, ValueError) as exc:
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
                "usage_count": 0,
                "last_used": asyncio.get_running_loop().time(),
                "healthy": True,
                "pid": _extract_browser_pid(new_browser),
            }
            logger.info("Replenished browser pool after prior replacement failure")
            return new_browser

    async def _pop_candidate_browser(self) -> Browser:
        try:
            return self._available_browsers.get_nowait()
        except asyncio.QueueEmpty:
            if len(self._all_browsers) < self.config.pool_size:
                replenished = await self._replenish_browser_if_needed()
                if replenished is not None:
                    return replenished
            return await asyncio.wait_for(
                self._available_browsers.get(), timeout=self.config.acquire_timeout
            )

    def _record_browser_acquired(self, browser: Browser) -> Browser:
        if browser in self._browser_health:
            self._browser_health[browser]["usage_count"] += 1
            self._browser_health[browser]["last_used"] = asyncio.get_running_loop().time()
        return browser

    async def get_browser(self) -> Browser:
        """Acquire a connected browser from the pool within `acquire_timeout`, replacing dead ones."""
        if self._closed:
            raise BrowserPoolError("Browser pool is closed")

        try:
            max_attempts = max(1, self.config.pool_size) + 1
            for _ in range(max_attempts):
                browser = await self._pop_candidate_browser()
                if await self._is_browser_healthy(browser):
                    return self._record_browser_acquired(browser)

                logger.warning(
                    "Discarding disconnected browser on acquire and launching replacement"
                )
                self._all_browsers.discard(browser)
                self._browser_health.pop(browser, None)
                await self._close_browser_with_timeout(browser)
                replenished = await self._replenish_browser_if_needed()
                if replenished is not None:
                    if await self._is_browser_healthy(replenished):
                        return self._record_browser_acquired(replenished)
                    self._all_browsers.discard(replenished)
                    self._browser_health.pop(replenished, None)
                    await self._close_browser_with_timeout(replenished)
            raise BrowserPoolError("Unable to acquire a connected browser after replacement")
        except TimeoutError as exc:
            raise BrowserAcquireTimeoutError(
                f"No browser available within timeout ({self.config.acquire_timeout}s)"
            ) from exc
        except BrowserPoolError:
            raise
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
        except (PlaywrightError, BrowserPoolError, OSError, RuntimeError, ValueError):
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
                "pid": _extract_browser_pid(new_browser),
            }
            await self._available_browsers.put(new_browser)
            logger.info("Successfully replaced unhealthy browser")
        except (PlaywrightError, BrowserPoolError, OSError, RuntimeError, ValueError) as exc:
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

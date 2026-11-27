"""PDF and screenshot generator backed by a lazy, idle-reaped `BrowserPool`."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser.manager import BrowserConfig, BrowserPool, BrowserPoolError
from .browser.session import (
    DEFAULT_BROWSER_USER_AGENT as DEFAULT_BROWSER_USER_AGENT,
)
from .browser.session import (
    BrowserSession as BrowserSession,
)
from .browser.session import (
    NavigationOptions,
    RedirectPolicyHook,
    navigate_and_validate,
    run_pre_capture_steps,
)
from .browser.session import (
    close_page_modals as close_page_modals,
)
from .browser.session import (
    install_ssrf_route_guard as install_ssrf_route_guard,
)
from .browser.session import (
    resolve_browser_user_agent as resolve_browser_user_agent,
)
from .browser.session import (
    validate_browser_navigation as validate_browser_navigation,
)
from .config import get_settings
from .models.browser_actions import BrowserAction, BrowserActionError
from .ports import (
    BrowserCapacityError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    PDFGeneratorError,
    RenderTimeoutError,
    RobotsDisallowedError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
    enforce_rendered_output_size,
)
from .telemetry import get_tracer
from .validation import EgressPolicy, URLValidationError

__all__ = [
    "BrowserSession",
    "DEFAULT_BROWSER_USER_AGENT",
    "PDFGeneratorError",
    "PlaywrightPDFGenerator",
    "RenderTimeoutError",
    "ScreenshotGeneratorError",
    "cleanup_pdf_generator",
    "close_page_modals",
    "enforce_rendered_output_size",
    "generate_pdf_from_url",
    "generate_screenshot_from_url",
    "get_pdf_generator",
    "get_shared_pdf_generator",
    "install_ssrf_route_guard",
    "resolve_browser_user_agent",
    "set_shared_pdf_generator",
    "validate_browser_navigation",
]

logger = logging.getLogger(__name__)


class PlaywrightPDFGenerator:
    """PDF and screenshot generator backed by a lazy, idle-reaped `BrowserPool`."""

    def __init__(  # noqa: PLR0913
        self,
        pool_size: int = 3,
        page_load_timeout: int = 30000,
        wait_until: str = "domcontentloaded",
        memory_limit_mb: int = 512,
        max_uses_per_browser: int = 50,
        idle_timeout: float = 600.0,
        user_agent: str | None = None,
        *,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
    ) -> None:
        self.pool: BrowserPool | None = None
        self._start_lock = asyncio.Lock()
        self.pool_config = BrowserConfig(
            pool_size=pool_size,
            memory_limit_mb=memory_limit_mb,
            max_uses_per_browser=max_uses_per_browser,
        )
        self.page_load_timeout = page_load_timeout
        self.wait_until = wait_until
        self.idle_timeout = idle_timeout
        self.user_agent = user_agent
        self._egress_policy = egress_policy
        self._redirect_policy = redirect_policy
        self._active_contexts: int = 0
        self._last_used_at: float = 0.0
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def pool_size(self) -> int:
        """Return configured browser pool size."""
        return self.pool_config.pool_size

    @property
    def available_browsers(self) -> int | None:
        """Return idle browsers in the active pool, or `None` when lazily uninitialized."""
        return self.pool.available_count if self.pool is not None else None

    def mark_context_acquired(self) -> None:
        """Record an active browser context and update last-used timestamp."""
        self._active_contexts += 1
        self._last_used_at = time.monotonic()

    def mark_context_released(self) -> None:
        """Record browser context release and update last-used timestamp."""
        self._active_contexts = max(0, self._active_contexts - 1)
        self._last_used_at = time.monotonic()

    async def __aenter__(self) -> PlaywrightPDFGenerator:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _cancel_idle_task(self) -> None:
        """Cancel and await the background idle reaper task if running."""
        if self._idle_task is not None:
            if not self._idle_task.done() and self._idle_task is not asyncio.current_task():
                self._idle_task.cancel()
                try:
                    await self._idle_task
                except asyncio.CancelledError:
                    pass
            self._idle_task = None

    async def _idle_reaper_loop(self) -> None:
        """Close the browser pool after `idle_timeout` with zero active contexts."""
        check_interval = max(min(self.idle_timeout / 2.0, 30.0), 0.05)
        try:
            while True:
                await asyncio.sleep(check_interval)
                if self.pool is None:
                    return
                if (
                    self._active_contexts == 0
                    and (time.monotonic() - self._last_used_at) >= self.idle_timeout
                ):
                    async with self._start_lock:
                        if (
                            self._active_contexts == 0
                            and self.pool is not None
                            and (time.monotonic() - self._last_used_at) >= self.idle_timeout
                        ):
                            pool_to_close = self.pool
                            self.pool = None
                            await pool_to_close.close()
                            logger.info("Browser pool closed after idle timeout")
                            return
        except asyncio.CancelledError:
            return

    async def start(self) -> None:
        """Initialize browser pool."""
        await self._cancel_idle_task()
        try:
            if self._egress_policy is not None:
                pool = BrowserPool(config=self.pool_config, egress_policy=self._egress_policy)
            else:
                pool = BrowserPool(config=self.pool_config)
            await pool.start()
            self.pool = pool
            self._last_used_at = time.monotonic()
            if self.idle_timeout > 0:
                self._idle_task = asyncio.create_task(self._idle_reaper_loop())
            logger.info("PDF generator initialized with browser pool")
        except BrowserPoolError as exc:
            self.pool = None
            logger.error(f"Failed to initialize PDF generator: {exc}")
            raise PDFGeneratorError(f"PDF generator initialization failed: {exc}") from exc

    async def ensure_started(self) -> BrowserPool:
        """Lazily initialize browser pool on first use under an async lock."""
        self._last_used_at = time.monotonic()
        if self.pool is not None:
            return self.pool
        async with self._start_lock:
            if self.pool is None:
                await self.start()
            else:
                self._last_used_at = time.monotonic()
        assert self.pool is not None
        return self.pool

    async def close(self) -> None:
        """Close browser pool and cleanup."""
        try:
            await self._cancel_idle_task()
            if self.pool:
                await self.pool.close()
                self.pool = None
            logger.info("PDF generator closed")
        except (PlaywrightError, BrowserPoolError, RuntimeError, OSError) as exc:
            logger.error(f"Error closing PDF generator: {exc}")

    async def generate_pdf(  # noqa: PLR0913
        self,
        url: str,
        options: dict[str, Any] | None = None,
        *,
        actions: Sequence[BrowserAction] | None = None,
        wait_for_selector: str | None = None,
        max_output_bytes: int | None = None,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
    ) -> bytes:
        """Generate PDF from URL with `BrowserSession` isolation and `max_download_size` check."""
        pdf_options: dict[str, Any] = {
            "format": "A4",
            "print_background": True,
            "margin": {"top": "20px", "right": "20px", "bottom": "20px", "left": "20px"},
            "wait_for": self.wait_until,
            "timeout": self.page_load_timeout,
        }
        if options:
            pdf_options.update(options)

        timeout_ms = int(pdf_options.get("timeout", 30000))
        active_egress = egress_policy if egress_policy is not None else self._egress_policy
        active_redirect = redirect_policy if redirect_policy is not None else self._redirect_policy
        cfg = active_egress.settings if active_egress is not None else get_settings()
        resolved_max_bytes = (
            max_output_bytes if max_output_bytes is not None else cfg.content.max_download_size
        )
        session_budget = (
            max(15.0, (float(timeout_ms) / 1000.0) * 2.0 + 10.0)
            if timeout_ms > self.page_load_timeout
            else None
        )
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("playwright.generate_pdf") as span:
            span.set_attribute("http.url", url)
            try:
                session = BrowserSession(
                    self,
                    egress_policy=active_egress,
                    redirect_policy=active_redirect,
                    timeout_seconds=session_budget,
                )
                async with session as (_context, page):
                    logger.info(f"Loading page: {url}")
                    await navigate_and_validate(
                        session,
                        url,
                        NavigationOptions(
                            wait_until=pdf_options.get("wait_for", "domcontentloaded"),
                            timeout_ms=timeout_ms,
                            settle_timeout_ms=min(timeout_ms, 2500),
                            error_kind="pdf",
                        ),
                    )
                    await run_pre_capture_steps(
                        session,
                        url,
                        wait_for_selector=wait_for_selector,
                        actions=actions,
                        timeout_ms=timeout_ms,
                    )
                    pdf_bytes: bytes = await page.pdf(
                        format=pdf_options.get("format", "A4"),
                        print_background=pdf_options.get("print_background", True),
                        margin=pdf_options["margin"],
                        prefer_css_page_size=True,
                        display_header_footer=False,
                    )
                    enforce_rendered_output_size(
                        pdf_bytes,
                        max_output_bytes=resolved_max_bytes,
                        egress_policy=active_egress,
                    )
                    span.set_attribute("pdf.bytes", len(pdf_bytes))
                    logger.info(f"PDF generated successfully, size: {len(pdf_bytes)} bytes")
                    return pdf_bytes
            except (
                PDFGeneratorError,
                BrowserActionError,
                BrowserCapacityError,
                SelectorTimeoutError,
                URLValidationError,
                RobotsDisallowedError,
                CrawlDelayExceededError,
                ContentTooLargeError,
            ):
                raise
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                logger.error(f"PDF generation timed out for {url}: {exc}")
                raise RenderTimeoutError(f"PDF generation timed out: {exc}") from exc
            except (PlaywrightError, BrowserPoolError, RuntimeError, OSError, ValueError) as exc:
                logger.error(f"Unexpected error generating PDF: {exc}")
                raise PDFGeneratorError(f"PDF generation failed: {exc}") from exc

    async def generate_screenshot(  # noqa: PLR0913
        self,
        url: str,
        options: dict[str, Any] | None = None,
        *,
        actions: Sequence[BrowserAction] | None = None,
        wait_for_selector: str | None = None,
        max_output_bytes: int | None = None,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
    ) -> bytes:
        """Capture a PNG or JPEG screenshot from a URL with `max_download_size` enforcement."""
        shot_options: dict[str, Any] = {
            "type": "png",
            "full_page": False,
            "timeout": self.page_load_timeout,
        }
        if options:
            shot_options.update(options)

        image_type = shot_options.get("type", "png")
        if image_type not in ("png", "jpeg"):
            raise ScreenshotGeneratorError(f"Unsupported screenshot format: {image_type}")

        timeout_ms = int(shot_options.get("timeout", self.page_load_timeout))
        active_egress = egress_policy if egress_policy is not None else self._egress_policy
        active_redirect = redirect_policy if redirect_policy is not None else self._redirect_policy
        cfg = active_egress.settings if active_egress is not None else get_settings()
        resolved_max_bytes = (
            max_output_bytes if max_output_bytes is not None else cfg.content.max_download_size
        )
        session_budget = (
            max(15.0, (float(timeout_ms) / 1000.0) * 2.0 + 10.0)
            if timeout_ms > self.page_load_timeout
            else None
        )
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("playwright.screenshot") as span:
            span.set_attribute("http.url", url)
            span.set_attribute("screenshot.type", image_type)
            span.set_attribute("screenshot.full_page", bool(shot_options.get("full_page", False)))
            try:
                session = BrowserSession(
                    self,
                    egress_policy=active_egress,
                    redirect_policy=active_redirect,
                    timeout_seconds=session_budget,
                )
                async with session as (_context, page):
                    await navigate_and_validate(
                        session,
                        url,
                        NavigationOptions(
                            wait_until="domcontentloaded",
                            timeout_ms=timeout_ms,
                            settle_timeout_ms=min(timeout_ms, 2500),
                            error_kind="screenshot",
                        ),
                    )
                    await run_pre_capture_steps(
                        session,
                        url,
                        wait_for_selector=wait_for_selector,
                        actions=actions,
                        timeout_ms=timeout_ms,
                    )
                    screenshot_kwargs: dict[str, Any] = {
                        "type": image_type,
                        "full_page": bool(shot_options.get("full_page", False)),
                    }
                    if image_type == "jpeg" and "quality" in shot_options:
                        screenshot_kwargs["quality"] = int(shot_options["quality"])

                    screenshot_bytes: bytes = await page.screenshot(**screenshot_kwargs)
                    enforce_rendered_output_size(
                        screenshot_bytes,
                        max_output_bytes=resolved_max_bytes,
                        egress_policy=active_egress,
                    )
                    span.set_attribute("screenshot.bytes", len(screenshot_bytes))
                    logger.info(
                        f"Screenshot captured ({image_type}), size: {len(screenshot_bytes)} bytes"
                    )
                    return screenshot_bytes
            except (
                PDFGeneratorError,
                BrowserActionError,
                BrowserCapacityError,
                SelectorTimeoutError,
                URLValidationError,
                RobotsDisallowedError,
                CrawlDelayExceededError,
                ContentTooLargeError,
            ):
                raise
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                logger.error(f"Screenshot capture timed out for {url}: {exc}")
                raise RenderTimeoutError(f"Screenshot capture timed out: {exc}") from exc
            except (PlaywrightError, BrowserPoolError, RuntimeError, OSError, ValueError) as exc:
                logger.error(f"Unexpected error capturing screenshot: {exc}")
                raise ScreenshotGeneratorError(f"Screenshot generation failed: {exc}") from exc


_pdf_generator: PlaywrightPDFGenerator | None = None
_initialization_lock = asyncio.Lock()


@asynccontextmanager
async def get_pdf_generator() -> AsyncIterator[PlaywrightPDFGenerator]:
    """Get or create the shared `PlaywrightPDFGenerator` instance."""
    global _pdf_generator

    async with _initialization_lock:
        if _pdf_generator is None:
            cfg = get_settings()
            generator = PlaywrightPDFGenerator(
                pool_size=cfg.pdf.pool_size,
                page_load_timeout=cfg.pdf.page_load_timeout,
                wait_until=cfg.pdf.wait_until,
                memory_limit_mb=cfg.pdf.browser_memory_limit_mb,
                max_uses_per_browser=cfg.pdf.max_uses_per_browser,
                idle_timeout=cfg.pdf.idle_timeout,
                user_agent=None,
                egress_policy=EgressPolicy(cfg),
            )
            await generator.start()
            _pdf_generator = generator

    try:
        assert _pdf_generator is not None
        yield _pdf_generator
    except (PDFGeneratorError, BrowserPoolError, PlaywrightError, RuntimeError, OSError) as exc:
        logger.error(f"Error in PDF generator: {exc}")
        raise


async def generate_pdf_from_url(  # noqa: PLR0913
    url: str,
    options: dict[str, Any] | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    wait_for_selector: str | None = None,
    max_output_bytes: int | None = None,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: RedirectPolicyHook | None = None,
) -> bytes:
    """Generate PDF from URL using shared generator instance."""
    async with get_pdf_generator() as generator:
        extra: dict[str, Any] = {}
        if egress_policy is not None:
            extra["egress_policy"] = egress_policy
        if redirect_policy is not None:
            extra["redirect_policy"] = redirect_policy
        return await generator.generate_pdf(
            url,
            options,
            actions=actions,
            wait_for_selector=wait_for_selector,
            max_output_bytes=max_output_bytes,
            **extra,
        )


async def generate_screenshot_from_url(  # noqa: PLR0913
    url: str,
    options: dict[str, Any] | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    wait_for_selector: str | None = None,
    max_output_bytes: int | None = None,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: RedirectPolicyHook | None = None,
) -> bytes:
    """Capture a screenshot from URL using shared generator instance."""
    async with get_pdf_generator() as generator:
        extra: dict[str, Any] = {}
        if egress_policy is not None:
            extra["egress_policy"] = egress_policy
        if redirect_policy is not None:
            extra["redirect_policy"] = redirect_policy
        return await generator.generate_screenshot(
            url,
            options,
            actions=actions,
            wait_for_selector=wait_for_selector,
            max_output_bytes=max_output_bytes,
            **extra,
        )


def set_shared_pdf_generator(generator: PlaywrightPDFGenerator | None) -> None:
    """Register or clear the shared PDF generator instance."""
    global _pdf_generator
    _pdf_generator = generator


def get_shared_pdf_generator() -> PlaywrightPDFGenerator | None:
    """Get the shared PDF generator instance if it exists."""
    return _pdf_generator


async def cleanup_pdf_generator() -> None:
    """Cleanup global PDF generator instance."""
    global _pdf_generator
    if _pdf_generator:
        await _pdf_generator.close()
        _pdf_generator = None

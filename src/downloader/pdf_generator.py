"""PDF generation using Playwright for JavaScript rendering."""

import asyncio
import inspect
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from .browser import BrowserConfig, BrowserPool, BrowserPoolError
from .config import get_settings
from .validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_ALLOWED_INLINE_SCHEMES = frozenset({"data", "blob", "about"})


async def _maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


def resolve_browser_user_agent(explicit_user_agent: str | None = None) -> str:
    """Resolve effective User-Agent for Playwright contexts."""
    if isinstance(explicit_user_agent, str) and explicit_user_agent.strip():
        return explicit_user_agent.strip()

    settings = get_settings()
    crawling = getattr(settings, "crawling", None)
    if crawling is not None:
        fields_set: set[str] = getattr(crawling, "model_fields_set", set())
        configured_ua = getattr(crawling, "user_agent", None)
        if (
            isinstance(configured_ua, str)
            and configured_ua.strip()
            and (
                "user_agent" in fields_set
                or "USER_AGENT" in os.environ
                or configured_ua.strip() != "REST-API-Downloader/1.0"
            )
        ):
            return configured_ua.strip()

    return DEFAULT_BROWSER_USER_AGENT


async def _ssrf_route_guard(route: Any) -> None:
    """Block SSRF and non-HTTP(S) subrequests inside Playwright browser contexts."""
    req_url = getattr(getattr(route, "request", None), "url", "") or ""
    parsed = urlparse(req_url)
    scheme = parsed.scheme.lower()

    if scheme in _ALLOWED_INLINE_SCHEMES:
        await _maybe_await(route.continue_())
        return

    if scheme not in ("http", "https"):
        logger.warning(f"Blocked browser request with disallowed scheme '{scheme}': {req_url}")
        await _maybe_await(route.abort("accessdenied"))
        return

    try:
        validate_url(req_url, get_settings())
    except URLValidationError as exc:
        logger.warning(f"Blocked browser request failing SSRF validation ({req_url}): {exc}")
        await _maybe_await(route.abort("accessdenied"))
        return

    await _maybe_await(route.continue_())


async def install_ssrf_route_guard(context: Any) -> None:
    """Register SSRF route interception on a Playwright BrowserContext."""
    route_fn = getattr(context, "route", None)
    if callable(route_fn):
        await _maybe_await(route_fn("**/*", _ssrf_route_guard))


class PDFGeneratorError(Exception):
    """Raised when PDF generation fails."""

    pass


class PlaywrightPDFGenerator:
    """PDF generator using Playwright with browser pooling for concurrent PDF generation."""

    def __init__(
        self,
        pool_size: int = 3,
        page_load_timeout: int = 30000,
        wait_until: str = "networkidle",
        memory_limit_mb: int = 512,
        max_uses_per_browser: int = 50,
        idle_timeout: float = 600.0,
        user_agent: str | None = None,
    ):
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
        self._active_contexts: int = 0
        self._last_used_at: float = 0.0
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def pool_size(self) -> int:
        """Get pool size for backward compatibility."""
        return self.pool_config.pool_size

    def mark_context_acquired(self) -> None:
        """Record an active browser context and update last-used timestamp."""
        self._active_contexts += 1
        self._last_used_at = time.monotonic()

    def mark_context_released(self) -> None:
        """Record browser context release and update last-used timestamp."""
        self._active_contexts = max(0, self._active_contexts - 1)
        self._last_used_at = time.monotonic()

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
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
        """Close the browser pool after idle_timeout with zero active contexts."""
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

    async def start(self):
        """Initialize browser pool."""
        await self._cancel_idle_task()
        try:
            pool = BrowserPool(config=self.pool_config)
            await pool.start()
            self.pool = pool
            self._last_used_at = time.monotonic()
            if self.idle_timeout > 0:
                self._idle_task = asyncio.create_task(self._idle_reaper_loop())
            logger.info("PDF generator initialized with browser pool")
        except BrowserPoolError as e:
            self.pool = None
            logger.error(f"Failed to initialize PDF generator: {e}")
            raise PDFGeneratorError(f"PDF generator initialization failed: {e}")

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

    async def close(self):
        """Close browser pool and cleanup."""
        try:
            await self._cancel_idle_task()
            if self.pool:
                await self.pool.close()
                self.pool = None
            logger.info("PDF generator closed")
        except Exception as e:
            logger.error(f"Error closing PDF generator: {e}")

    @asynccontextmanager
    async def _get_browser_context(self):
        """Async context manager for automatic browser resource cleanup."""
        self.mark_context_acquired()
        pool = None
        browser = None
        context = None
        try:
            pool = await self.ensure_started()

            # Get browser from pool with O(1) selection
            browser = await pool.get_browser()
            self._last_used_at = time.monotonic()

            # Create isolated context for this request (security)
            context = await browser.new_context(
                user_agent=resolve_browser_user_agent(self.user_agent),
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=False,  # Security: don't ignore HTTPS errors
                java_script_enabled=True,
                bypass_csp=False,  # Security: respect Content Security Policy
            )
            await install_ssrf_route_guard(context)

            yield browser, context

        finally:
            # Automatic cleanup - guaranteed to run even on exceptions
            if context:
                try:
                    await context.close()  # This closes all pages in context
                except Exception as e:
                    logger.warning(f"Error closing browser context: {e}")

            active_pool = self.pool or pool
            if browser and active_pool:
                try:
                    await active_pool.release_browser(browser)
                except Exception as e:
                    logger.warning(f"Error releasing browser to pool: {e}")

            self.mark_context_released()

    async def generate_pdf(self, url: str, options: dict[str, Any] | None = None) -> bytes:
        """
        Generate PDF from URL using optimized browser pool with automatic resource cleanup.

        Args:
            url: The URL to convert to PDF
            options: PDF generation options

        Returns:
            PDF content as bytes

        Raises:
            PDFGeneratorError: If PDF generation fails
        """
        # Default PDF options - use instance settings
        pdf_options = {
            "format": "A4",
            "print_background": True,
            "margin": {
                "top": "20px",
                "right": "20px",
                "bottom": "20px",
                "left": "20px",
            },
            "wait_for": self.wait_until,
            "timeout": self.page_load_timeout,
        }

        # Override with custom options
        if options:
            pdf_options.update(options)

        try:
            # Use automatic resource cleanup with async context manager
            async with self._get_browser_context() as (browser, context):
                # Create page in isolated context
                page = await context.new_page()
                try:
                    logger.info(f"Loading page: {url}")

                    # Navigate to page with timeout
                    try:
                        response = await page.goto(
                            url,
                            wait_until=pdf_options.get("wait_for", "networkidle"),
                            timeout=pdf_options.get("timeout", 30000),
                        )

                        if not response:
                            raise PDFGeneratorError(f"Failed to load page: {url}")

                        if response.status >= 400:
                            raise PDFGeneratorError(
                                f"HTTP {response.status}: {response.status_text}"
                            )

                    except TimeoutError:
                        raise PDFGeneratorError(f"Timeout loading page: {url}")

                    # Wait for page to be fully loaded
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=pdf_options.get("timeout", 30000),
                    )

                    # Try to close any signup boxes/modals
                    await self._close_modals(page)

                    # Additional wait for dynamic content
                    await asyncio.sleep(2)

                    logger.info("Generating PDF...")

                    # Generate PDF with specified options
                    pdf_bytes = await page.pdf(
                        format=pdf_options.get("format", "A4"),
                        print_background=pdf_options.get("print_background", True),
                        margin=pdf_options.get(
                            "margin",
                            {
                                "top": "20px",
                                "right": "20px",
                                "bottom": "20px",
                                "left": "20px",
                            },
                        ),
                        prefer_css_page_size=True,
                        display_header_footer=False,
                    )

                    logger.info(f"PDF generated successfully, size: {len(pdf_bytes)} bytes")
                    return pdf_bytes

                finally:
                    # Close page resources
                    if page:
                        try:
                            await page.close()
                        except Exception:
                            pass  # Ignore errors during cleanup

        except PDFGeneratorError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error generating PDF: {e}")
            raise PDFGeneratorError(f"PDF generation failed: {e}")

    async def _close_modals(self, page):
        """Helper method to close common modal dialogs."""
        try:
            close_selectors = [
                '[aria-label="close"]',
                '[title="Close"]',
                '[aria-label="Close"]',
                '[title="close"]',
                ".modal-close",
                ".close-button",
            ]
            for selector in close_selectors:
                close_buttons = await page.query_selector_all(selector)
                for button in close_buttons:
                    try:
                        await button.click(timeout=1000)
                        logger.debug(f"Closed modal/popup with selector: {selector}")
                        await page.wait_for_timeout(500)  # Brief wait after closing
                    except Exception:
                        pass  # Ignore if click fails
        except Exception:
            pass  # Ignore any errors during modal closing


# Global instance for reuse
_pdf_generator: PlaywrightPDFGenerator | None = None
_initialization_lock = asyncio.Lock()


@asynccontextmanager
async def get_pdf_generator():
    """
    Get or create a shared PDF generator instance.

    Yields:
        PlaywrightPDFGenerator: The PDF generator instance
    """
    global _pdf_generator

    # Use lock to prevent multiple simultaneous initializations
    async with _initialization_lock:
        if _pdf_generator is None:
            generator = PlaywrightPDFGenerator(pool_size=2)  # Reduce pool size for Docker
            await generator.start()
            _pdf_generator = generator

    try:
        yield _pdf_generator
    except Exception as e:
        logger.error(f"Error in PDF generator: {e}")
        # Don't recreate on every error - only close if it's a severe error
        if "initialization failed" in str(e).lower():
            async with _initialization_lock:
                if _pdf_generator:
                    await _pdf_generator.close()
                    _pdf_generator = None
        raise


async def generate_pdf_from_url(url: str, options: dict[str, Any] | None = None) -> bytes:
    """
    Generate PDF from URL using shared generator instance.

    Args:
        url: The URL to convert to PDF
        options: PDF generation options

    Returns:
        PDF content as bytes

    Raises:
        PDFGeneratorError: If PDF generation fails
    """
    async with get_pdf_generator() as generator:
        return await generator.generate_pdf(url, options)


def set_shared_pdf_generator(generator: PlaywrightPDFGenerator | None) -> None:
    """Register or clear the shared PDF generator instance."""
    global _pdf_generator
    _pdf_generator = generator


def get_shared_pdf_generator() -> PlaywrightPDFGenerator | None:
    """Get the shared PDF generator instance if it exists."""
    return _pdf_generator


async def cleanup_pdf_generator():
    """Cleanup global PDF generator instance."""
    global _pdf_generator
    if _pdf_generator:
        await _pdf_generator.close()
        _pdf_generator = None

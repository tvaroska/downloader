"""PDF generation using Playwright for JavaScript rendering."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from playwright.async_api import Browser, async_playwright

logger = logging.getLogger(__name__)


class PDFGeneratorError(Exception):
    """Raised when PDF generation fails."""

    pass


class BrowserPool:
    """Optimized pool of browser instances for concurrent PDF generation.

    Uses queue-based approach for O(1) browser selection instead of O(n) linear search.
    Implements automatic health monitoring and dynamic pool management.
    """

    def __init__(self, pool_size: int = 3):
        self.pool_size = pool_size
        self._available_browsers = asyncio.Queue(maxsize=pool_size)
        self._all_browsers: set[Browser] = set()
        self._browser_health: dict[Browser, dict] = {}
        self._playwright = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def start(self):
        """Initialize the browser pool with queue-based management."""
        try:
            self._playwright = await async_playwright().start()
            self._closed = False

            # Initialize browsers and add them to the available queue
            for i in range(self.pool_size):
                browser = await self._launch_browser()
                self._all_browsers.add(browser)
                self._browser_health[browser] = {
                    "usage_count": 0,
                    "last_used": asyncio.get_event_loop().time(),
                    "healthy": True,
                }
                await self._available_browsers.put(browser)
                logger.info(f"Browser {i + 1}/{self.pool_size} initialized")

            logger.info(f"Optimized browser pool initialized with {self.pool_size} browsers")
        except Exception as e:
            logger.error(f"Failed to initialize browser pool: {e}")
            raise PDFGeneratorError(f"Browser pool initialization failed: {e}")

    async def _launch_browser(self) -> Browser:
        """Launch a single browser instance with automatic browser detection."""
        return await self._playwright.chromium.launch(
            headless=True,
            # Remove hardcoded paths - let Playwright auto-detect browser
            args=[
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
                "--disable-web-security",  # For better compatibility
            ],
        )

    async def get_browser(self) -> Browser:
        """Get an available browser from the pool with O(1) selection."""
        if self._closed:
            raise PDFGeneratorError("Browser pool is closed")

        try:
            # O(1) browser selection using queue
            browser = await asyncio.wait_for(self._available_browsers.get(), timeout=30.0)

            # Update health metrics
            if browser in self._browser_health:
                self._browser_health[browser]["usage_count"] += 1
                self._browser_health[browser]["last_used"] = asyncio.get_event_loop().time()

            return browser
        except asyncio.TimeoutError:
            raise PDFGeneratorError("No browser available within timeout (30s)")
        except Exception as e:
            raise PDFGeneratorError(f"Failed to get browser from pool: {e}")

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
            # Remove old browser
            self._all_browsers.discard(old_browser)
            self._browser_health.pop(old_browser, None)

            # Close old browser safely
            try:
                if old_browser.is_connected():
                    await old_browser.close()
            except Exception:
                pass  # Ignore errors when closing broken browser

            # Create new browser
            new_browser = await self._launch_browser()
            self._all_browsers.add(new_browser)
            self._browser_health[new_browser] = {
                "usage_count": 0,
                "last_used": asyncio.get_event_loop().time(),
                "healthy": True,
            }

            # Add to available queue
            await self._available_browsers.put(new_browser)
            logger.info("Successfully replaced unhealthy browser")
        except Exception as e:
            logger.error(f"Failed to replace browser: {e}")

    async def close(self):
        """Close all browsers and cleanup."""
        self._closed = True

        try:
            # Close all browsers
            close_tasks = []
            for browser in self._all_browsers:
                if browser and browser.is_connected():
                    close_tasks.append(browser.close())

            if close_tasks:
                await asyncio.gather(*close_tasks, return_exceptions=True)

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

            logger.info("Optimized browser pool closed")
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
        }


class PlaywrightPDFGenerator:
    """PDF generator using Playwright with browser pooling for concurrent PDF generation."""

    def __init__(
        self,
        pool_size: int = 3,
        page_load_timeout: int = 30000,
        wait_until: str = "networkidle",
    ):
        self.pool: BrowserPool | None = None
        self.pool_size = pool_size
        self.page_load_timeout = page_load_timeout
        self.wait_until = wait_until

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def start(self):
        """Initialize browser pool."""
        try:
            self.pool = BrowserPool(pool_size=self.pool_size)
            await self.pool.start()
            logger.info("PDF generator initialized with browser pool")
        except Exception as e:
            logger.error(f"Failed to initialize PDF generator: {e}")
            raise PDFGeneratorError(f"PDF generator initialization failed: {e}")

    async def close(self):
        """Close browser pool and cleanup."""
        try:
            if self.pool:
                await self.pool.close()
                self.pool = None
            logger.info("PDF generator closed")
        except Exception as e:
            logger.error(f"Error closing PDF generator: {e}")

    @asynccontextmanager
    async def _get_browser_context(self):
        """Async context manager for automatic browser resource cleanup."""
        if not self.pool:
            raise PDFGeneratorError("Browser pool not initialized. Call start() first.")

        browser = None
        context = None
        try:
            # Get browser from pool with O(1) selection
            browser = await self.pool.get_browser()

            # Create isolated context for this request (security)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=False,  # Security: don't ignore HTTPS errors
                java_script_enabled=True,
                bypass_csp=False,  # Security: respect Content Security Policy
            )

            yield browser, context

        finally:
            # Automatic cleanup - guaranteed to run even on exceptions
            if context:
                try:
                    await context.close()  # This closes all pages in context
                except Exception as e:
                    logger.warning(f"Error closing browser context: {e}")

            if browser and self.pool:
                try:
                    await self.pool.release_browser(browser)
                except Exception as e:
                    logger.warning(f"Error releasing browser to pool: {e}")

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

                    except asyncio.TimeoutError:
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
            _pdf_generator = PlaywrightPDFGenerator(pool_size=2)  # Reduce pool size for Docker
            await _pdf_generator.start()

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


def get_shared_pdf_generator() -> PlaywrightPDFGenerator | None:
    """Get the shared PDF generator instance if it exists."""
    return _pdf_generator


async def cleanup_pdf_generator():
    """Cleanup global PDF generator instance."""
    global _pdf_generator
    if _pdf_generator:
        await _pdf_generator.close()
        _pdf_generator = None

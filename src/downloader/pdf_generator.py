"""PDF generation using Playwright for JavaScript rendering."""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from .browser import BrowserConfig, BrowserPool, BrowserPoolError

logger = logging.getLogger(__name__)


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
    ):
        self.pool: BrowserPool | None = None
        self.pool_config = BrowserConfig(
            pool_size=pool_size,
            memory_limit_mb=memory_limit_mb,
        )
        self.page_load_timeout = page_load_timeout
        self.wait_until = wait_until

    @property
    def pool_size(self) -> int:
        """Get pool size for backward compatibility."""
        return self.pool_config.pool_size

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
            self.pool = BrowserPool(config=self.pool_config)
            await self.pool.start()
            logger.info("PDF generator initialized with browser pool")
        except BrowserPoolError as e:
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

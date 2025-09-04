"""PDF generation using Playwright for JavaScript rendering."""

import asyncio
import logging
from typing import Dict, Any, Optional, List
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, Page, BrowserContext

logger = logging.getLogger(__name__)


class PDFGeneratorError(Exception):
    """Raised when PDF generation fails."""
    pass


class BrowserPool:
    """Pool of browser instances for concurrent PDF generation."""
    
    def __init__(self, pool_size: int = 3):
        self.pool_size = pool_size
        self.browsers: List[Browser] = []
        self.browser_usage: Dict[Browser, int] = {}
        self.current_index = 0
        self._playwright = None
        self._lock = asyncio.Lock()
        
    async def start(self):
        """Initialize the browser pool."""
        try:
            self._playwright = await async_playwright().start()
            
            for i in range(self.pool_size):
                browser = await self._launch_browser()
                self.browsers.append(browser)
                self.browser_usage[browser] = 0
                logger.info(f"Browser {i+1}/{self.pool_size} initialized")
                
            logger.info(f"Browser pool initialized with {self.pool_size} browsers")
        except Exception as e:
            logger.error(f"Failed to initialize browser pool: {e}")
            raise PDFGeneratorError(f"Browser pool initialization failed: {e}")
            
    async def _launch_browser(self) -> Browser:
        """Launch a single browser instance."""
        # Force use of full chromium instead of headless shell
        import os
        chromium_path = "/app/.playwright/chromium-1187/chrome-linux/chrome"
        
        return await self._playwright.chromium.launch(
            headless=True,
            executable_path=chromium_path if os.path.exists(chromium_path) else None,
            args=[
                '--no-sandbox',  # Required for containers
                '--disable-dev-shm-usage',  # Required for containers
                '--disable-gpu',  # Not needed for PDF
                # REMOVED: '--disable-web-security',  # Security risk - removed
                '--disable-features=VizDisplayCompositor',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-field-trial-config',
                '--disable-ipc-flooding-protection',
                '--virtual-time-budget=30000',  # Timeout protection
                '--run-all-compositor-stages-before-draw',
                '--disable-checker-imaging',
                '--disable-extensions',
                '--disable-plugins',
                '--disable-default-apps',
                '--disable-remote-fonts',  # Reduce resource usage
            ]
        )
        
    async def get_browser(self) -> Browser:
        """Get the least used browser from the pool."""
        async with self._lock:
            if not self.browsers:
                raise PDFGeneratorError("Browser pool not initialized")
                
            # Find browser with least usage
            min_usage = min(self.browser_usage.values())
            for browser in self.browsers:
                if self.browser_usage[browser] == min_usage:
                    self.browser_usage[browser] += 1
                    return browser
                    
            # Fallback to round-robin
            browser = self.browsers[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.browsers)
            self.browser_usage[browser] += 1
            return browser
            
    async def release_browser(self, browser: Browser):
        """Release a browser back to the pool."""
        async with self._lock:
            if browser in self.browser_usage:
                self.browser_usage[browser] = max(0, self.browser_usage[browser] - 1)
                
    async def close(self):
        """Close all browsers and cleanup."""
        try:
            for browser in self.browsers:
                if browser:
                    await browser.close()
            self.browsers.clear()
            self.browser_usage.clear()
            
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
                
            logger.info("Browser pool closed")
        except Exception as e:
            logger.error(f"Error closing browser pool: {e}")


class PlaywrightPDFGenerator:
    """PDF generator using Playwright with browser pooling for concurrent PDF generation."""
    
    def __init__(self, pool_size: int = 3):
        self.pool: Optional[BrowserPool] = None
        self.pool_size = pool_size
        
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
            
    async def generate_pdf(
        self, 
        url: str, 
        options: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """
        Generate PDF from URL using browser pool with context isolation.
        
        Args:
            url: The URL to convert to PDF
            options: PDF generation options
            
        Returns:
            PDF content as bytes
            
        Raises:
            PDFGeneratorError: If PDF generation fails
        """
        if not self.pool:
            raise PDFGeneratorError("Browser pool not initialized. Call start() first.")
            
        # Default PDF options
        pdf_options = {
            'format': 'A4',
            'print_background': True,
            'margin': {
                'top': '20px',
                'right': '20px',
                'bottom': '20px',
                'left': '20px'
            },
            'wait_for': 'networkidle',
            'timeout': 30000,  # 30 seconds
        }
        
        # Override with custom options
        if options:
            pdf_options.update(options)
            
        browser = None
        context = None
        page = None
        
        try:
            # Get browser from pool
            browser = await self.pool.get_browser()
            
            # Create isolated context for this request (security)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1280, 'height': 720},
                ignore_https_errors=False,  # Security: don't ignore HTTPS errors
                java_script_enabled=True,
                bypass_csp=False  # Security: respect Content Security Policy
            )
            
            # Create page in isolated context
            page = await context.new_page()
            
            logger.info(f"Loading page: {url}")
            
            # Navigate to page with timeout
            try:
                response = await page.goto(
                    url, 
                    wait_until=pdf_options.get('wait_for', 'networkidle'),
                    timeout=pdf_options.get('timeout', 30000)
                )
                
                if not response:
                    raise PDFGeneratorError(f"Failed to load page: {url}")
                    
                if response.status >= 400:
                    raise PDFGeneratorError(f"HTTP {response.status}: {response.status_text}")
                    
            except asyncio.TimeoutError:
                raise PDFGeneratorError(f"Timeout loading page: {url}")
                
            # Wait for page to be fully loaded
            await page.wait_for_load_state('networkidle', timeout=pdf_options.get('timeout', 30000))
            
            # Additional wait for dynamic content
            await asyncio.sleep(2)
            
            logger.info("Generating PDF...")
            
            # Generate PDF with specified options
            pdf_bytes = await page.pdf(
                format=pdf_options.get('format', 'A4'),
                print_background=pdf_options.get('print_background', True),
                margin=pdf_options.get('margin', {
                    'top': '20px',
                    'right': '20px', 
                    'bottom': '20px',
                    'left': '20px'
                }),
                prefer_css_page_size=True,
                display_header_footer=False,
            )
            
            logger.info(f"PDF generated successfully, size: {len(pdf_bytes)} bytes")
            return pdf_bytes
            
        except PDFGeneratorError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error generating PDF: {e}")
            raise PDFGeneratorError(f"PDF generation failed: {e}")
        finally:
            # Cleanup in reverse order
            if context:
                await context.close()  # This closes all pages in context
            if browser and self.pool:
                await self.pool.release_browser(browser)


# Global instance for reuse
_pdf_generator: Optional[PlaywrightPDFGenerator] = None
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


async def generate_pdf_from_url(url: str, options: Optional[Dict[str, Any]] = None) -> bytes:
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


async def cleanup_pdf_generator():
    """Cleanup global PDF generator instance."""
    global _pdf_generator
    if _pdf_generator:
        await _pdf_generator.close()
        _pdf_generator = None
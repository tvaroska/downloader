"""PDF generation using Playwright for JavaScript rendering."""

import asyncio
import logging
from typing import Dict, Any, Optional
from contextlib import asynccontextmanager

from playwright.async_api import async_playwright, Browser, Page

logger = logging.getLogger(__name__)


class PDFGeneratorError(Exception):
    """Raised when PDF generation fails."""
    pass


class PlaywrightPDFGenerator:
    """PDF generator using Playwright for full JavaScript rendering."""
    
    def __init__(self):
        self._browser: Optional[Browser] = None
        self._playwright = None
        
    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
        
    async def start(self):
        """Initialize Playwright and browser."""
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-background-timer-throttling',
                    '--disable-backgrounding-occluded-windows',
                    '--disable-renderer-backgrounding',
                    '--disable-field-trial-config',
                    '--disable-ipc-flooding-protection',
                ]
            )
            logger.info("Playwright browser initialized")
        except Exception as e:
            logger.error(f"Failed to initialize Playwright: {e}")
            raise PDFGeneratorError(f"Browser initialization failed: {e}")
            
    async def close(self):
        """Close browser and cleanup."""
        try:
            if self._browser:
                await self._browser.close()
                self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
            logger.info("Playwright browser closed")
        except Exception as e:
            logger.error(f"Error closing browser: {e}")
            
    async def generate_pdf(
        self, 
        url: str, 
        options: Optional[Dict[str, Any]] = None
    ) -> bytes:
        """
        Generate PDF from URL using Playwright.
        
        Args:
            url: The URL to convert to PDF
            options: PDF generation options
            
        Returns:
            PDF content as bytes
            
        Raises:
            PDFGeneratorError: If PDF generation fails
        """
        if not self._browser:
            raise PDFGeneratorError("Browser not initialized. Call start() first.")
            
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
            
        page = None
        try:
            # Create new page
            page = await self._browser.new_page()
            
            # Set viewport for consistent rendering
            await page.set_viewport_size({"width": 1280, "height": 720})
            
            # Set user agent
            await page.set_extra_http_headers({
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            })
            
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
            if page:
                await page.close()


# Global instance for reuse
_pdf_generator: Optional[PlaywrightPDFGenerator] = None


@asynccontextmanager
async def get_pdf_generator():
    """
    Get or create a shared PDF generator instance.
    
    Yields:
        PlaywrightPDFGenerator: The PDF generator instance
    """
    global _pdf_generator
    
    if _pdf_generator is None:
        _pdf_generator = PlaywrightPDFGenerator()
        await _pdf_generator.start()
        
    try:
        yield _pdf_generator
    except Exception as e:
        logger.error(f"Error in PDF generator: {e}")
        # Close and recreate on error
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
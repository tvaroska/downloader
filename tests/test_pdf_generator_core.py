"""Tests for core PDF generation functionality."""

from unittest.mock import AsyncMock

import pytest

from src.downloader.pdf_generator import PDFGeneratorError, PlaywrightPDFGenerator


class TestPlaywrightPDFGenerator:
    """Test PlaywrightPDFGenerator class core functionality."""

    @pytest.mark.asyncio
    async def test_init_and_start(self, mock_playwright):
        """Test initialization and starting of PDF generator."""
        mock, playwright_instance, browser = mock_playwright

        generator = PlaywrightPDFGenerator(pool_size=2)
        assert generator.pool is None
        assert generator.pool_size == 2

        await generator.start()

        mock.return_value.start.assert_called_once()
        assert playwright_instance.chromium.launch.call_count == 2
        assert generator.pool is not None

    @pytest.mark.asyncio
    async def test_start_failure(self, mock_playwright):
        """Test failure during start."""
        mock, _, _ = mock_playwright
        mock.return_value.start.side_effect = Exception("Browser start failed")

        generator = PlaywrightPDFGenerator()

        with pytest.raises(PDFGeneratorError, match="PDF generator initialization failed"):
            await generator.start()

    @pytest.mark.asyncio
    async def test_close(self, mock_playwright):
        """Test closing of PDF generator."""
        mock, playwright_instance, browser = mock_playwright

        generator = PlaywrightPDFGenerator()
        await generator.start()

        await generator.close()

        # Verify all browsers were launched
        assert playwright_instance.chromium.launch.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()
        assert generator.pool is None

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_playwright):
        """Test using as context manager."""
        mock, playwright_instance, browser = mock_playwright

        async with PlaywrightPDFGenerator() as generator:
            assert generator.pool is not None

        # Verify all browsers were launched
        assert playwright_instance.chromium.launch.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_pdf_lazy_initializes_pool(self, mock_browser_pool):
        """Test PDF generation lazily initializes browser pool when not yet started."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        response = AsyncMock(status=200, status_text="OK")
        page.goto = AsyncMock(return_value=response)
        page.pdf = AsyncMock(return_value=b"%PDF-1.4 lazy")

        generator = PlaywrightPDFGenerator()
        assert generator.pool is None

        result = await generator.generate_pdf("https://example.com")

        assert result == b"%PDF-1.4 lazy"
        assert generator.pool is pool_instance
        pool_instance.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_started_concurrent_single_init(self, mock_playwright):
        """Test ensure_started() initializes the browser pool only once under concurrency."""
        import asyncio

        mock, _, _ = mock_playwright
        generator = PlaywrightPDFGenerator(pool_size=1)
        pools = await asyncio.gather(
            generator.ensure_started(),
            generator.ensure_started(),
            generator.ensure_started(),
        )
        assert pools[0] is pools[1] is pools[2]
        mock.return_value.start.assert_called_once()
        await generator.close()

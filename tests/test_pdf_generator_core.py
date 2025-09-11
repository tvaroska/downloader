"""Tests for core PDF generation functionality."""


import pytest

from src.downloader.pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
)


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

        with pytest.raises(
            PDFGeneratorError, match="PDF generator initialization failed"
        ):
            await generator.start()

    @pytest.mark.asyncio
    async def test_close(self, mock_playwright):
        """Test closing of PDF generator."""
        mock, playwright_instance, browser = mock_playwright

        generator = PlaywrightPDFGenerator()
        await generator.start()

        await generator.close()

        assert browser.close.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()
        assert generator.pool is None

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_playwright):
        """Test using as context manager."""
        mock, playwright_instance, browser = mock_playwright

        async with PlaywrightPDFGenerator() as generator:
            assert generator.pool is not None

        assert browser.close.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_pdf_not_initialized(self):
        """Test PDF generation without initialization."""
        generator = PlaywrightPDFGenerator()

        with pytest.raises(PDFGeneratorError, match="Browser pool not initialized"):
            await generator.generate_pdf("https://example.com")

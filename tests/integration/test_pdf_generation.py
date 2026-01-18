"""Integration tests for PDF generation with Playwright."""

from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
    generate_pdf_from_url,
    get_pdf_generator,
)


@pytest.mark.integration
@pytest.mark.requires_playwright
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
    async def test_generate_pdf_not_initialized(self):
        """Test PDF generation without initialization."""
        generator = PlaywrightPDFGenerator()

        with pytest.raises(PDFGeneratorError, match="Browser pool not initialized"):
            await generator.generate_pdf("https://example.com")


@pytest.mark.integration
@pytest.mark.requires_playwright
class TestPDFGeneratorBrowserPool:
    """Test browser pool management in PDF generator."""

    @pytest.mark.asyncio
    async def test_generate_pdf_success(self, mock_browser_pool):
        """Test successful PDF generation."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

        response = AsyncMock()
        response.status = 200
        response.status_text = "OK"
        page.goto = AsyncMock(return_value=response)

        pdf_content = b"PDF content"
        page.pdf = AsyncMock(return_value=pdf_content)

        generator = PlaywrightPDFGenerator()
        await generator.start()

        result = await generator.generate_pdf("https://example.com")

        assert result == pdf_content
        pool_instance.get_browser.assert_called_once()
        browser.new_context.assert_called_once()
        context.new_page.assert_called_once()
        page.goto.assert_called_once()
        page.wait_for_load_state.assert_called_once()
        page.pdf.assert_called_once()
        context.close.assert_called_once()
        pool_instance.release_browser.assert_called_once_with(browser)

    @pytest.mark.asyncio
    async def test_generate_pdf_http_error(self, mock_browser_pool):
        """Test PDF generation with HTTP error."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

        response = AsyncMock()
        response.status = 404
        response.status_text = "Not Found"
        page.goto = AsyncMock(return_value=response)

        generator = PlaywrightPDFGenerator()
        await generator.start()

        with pytest.raises(PDFGeneratorError, match="HTTP 404"):
            await generator.generate_pdf("https://example.com/notfound")

        context.close.assert_called_once()
        pool_instance.release_browser.assert_called_once_with(browser)

    @pytest.mark.asyncio
    async def test_generate_pdf_no_response(self, mock_browser_pool):
        """Test PDF generation with no response."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        page.goto = AsyncMock(return_value=None)

        generator = PlaywrightPDFGenerator()
        await generator.start()

        with pytest.raises(PDFGeneratorError, match="Failed to load page"):
            await generator.generate_pdf("https://example.com")

        context.close.assert_called_once()
        pool_instance.release_browser.assert_called_once_with(browser)

    @pytest.mark.asyncio
    async def test_generate_pdf_custom_options(self, mock_browser_pool):
        """Test PDF generation with custom options."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page = AsyncMock()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

        response = AsyncMock()
        response.status = 200
        page.goto = AsyncMock(return_value=response)

        pdf_content = b"PDF content"
        page.pdf = AsyncMock(return_value=pdf_content)

        generator = PlaywrightPDFGenerator()
        await generator.start()

        custom_options = {
            "format": "Letter",
            "timeout": 60000,
            "margin": {"top": "10px", "bottom": "10px"},
        }

        result = await generator.generate_pdf("https://example.com", custom_options)

        assert result == pdf_content

        page.goto.assert_called_once()
        call_args = page.goto.call_args
        assert call_args[1]["timeout"] == 60000

        page.pdf.assert_called_once()
        pdf_call_args = page.pdf.call_args[1]
        assert pdf_call_args["format"] == "Letter"
        assert pdf_call_args["margin"]["top"] == "10px"

        context.close.assert_called_once()
        pool_instance.release_browser.assert_called_once_with(browser)


@pytest.mark.integration
@pytest.mark.requires_playwright
class TestGlobalPDFFunctions:
    """Test global PDF generation functions."""

    @pytest.mark.asyncio
    async def test_generate_pdf_from_url(self):
        """Test generate_pdf_from_url function."""
        with patch("src.downloader.pdf_generator.get_pdf_generator") as mock_get_generator:
            mock_generator = AsyncMock()
            mock_generator.generate_pdf = AsyncMock(return_value=b"PDF content")

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_generator)
            mock_context.__aexit__ = AsyncMock(return_value=None)
            mock_get_generator.return_value = mock_context

            result = await generate_pdf_from_url("https://example.com")

            assert result == b"PDF content"
            mock_generator.generate_pdf.assert_called_once_with("https://example.com", None)

    @pytest.mark.asyncio
    async def test_get_pdf_generator_creates_new(self):
        """Test that get_pdf_generator creates new instance when none exists."""
        import src.downloader.pdf_generator

        src.downloader.pdf_generator._pdf_generator = None

        with patch("src.downloader.pdf_generator.PlaywrightPDFGenerator") as mock_class:
            mock_instance = AsyncMock()
            mock_class.return_value = mock_instance

            async with get_pdf_generator() as generator:
                assert generator == mock_instance
                mock_instance.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pdf_generator_reuses_existing(self):
        """Test that get_pdf_generator reuses existing instance."""
        import src.downloader.pdf_generator

        existing_instance = AsyncMock()
        src.downloader.pdf_generator._pdf_generator = existing_instance

        async with get_pdf_generator() as generator:
            assert generator == existing_instance
            existing_instance.start.assert_not_called()

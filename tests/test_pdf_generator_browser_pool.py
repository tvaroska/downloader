"""Tests for PDF generator browser pool functionality."""

from unittest.mock import AsyncMock

import pytest

from src.downloader.pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
)


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

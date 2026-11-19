"""Unit tests for `PlaywrightPDFGenerator` lifecycle and options using mocked Playwright."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
    generate_pdf_from_url,
    get_pdf_generator,
)


def _mock_page_and_response(
    status: int = 200, status_text: str = "OK"
) -> tuple[AsyncMock, MagicMock]:
    page = AsyncMock()
    page.url = "https://example.com"
    response = MagicMock()
    response.status = status
    response.status_text = status_text
    response.url = "https://example.com"
    response.request = None
    page.goto = AsyncMock(return_value=response)
    return page, response


@pytest.mark.unit
class TestPlaywrightPDFGenerator:
    """Test PlaywrightPDFGenerator class core functionality."""

    @pytest.mark.asyncio
    async def test_init_and_start(self, mock_playwright):
        """Test initialization and starting of PDF generator."""
        mock, playwright_instance, _ = mock_playwright

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
        mock.return_value.start.side_effect = RuntimeError("Browser start failed")

        generator = PlaywrightPDFGenerator()

        with pytest.raises(PDFGeneratorError, match="PDF generator initialization failed"):
            await generator.start()

    @pytest.mark.asyncio
    async def test_close(self, mock_playwright):
        """Test closing of PDF generator."""
        _, playwright_instance, _ = mock_playwright

        generator = PlaywrightPDFGenerator()
        await generator.start()

        await generator.close()

        assert playwright_instance.chromium.launch.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()
        assert generator.pool is None

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_playwright):
        """Test using as context manager."""
        _, playwright_instance, _ = mock_playwright

        async with PlaywrightPDFGenerator() as generator:
            assert generator.pool is not None

        assert playwright_instance.chromium.launch.call_count == generator.pool_size
        playwright_instance.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_pdf_lazy_initializes_pool(self, mock_browser_pool):
        """Test PDF generation lazily initializes browser pool when not yet started."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page, _ = _mock_page_and_response()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)
        page.pdf = AsyncMock(return_value=b"%PDF-1.4 lazy")

        generator = PlaywrightPDFGenerator()
        assert generator.pool is None

        result = await generator.generate_pdf("https://example.com")

        assert result == b"%PDF-1.4 lazy"
        pool_instance.start.assert_called_once()
        assert generator.pool is pool_instance


@pytest.mark.unit
class TestPDFGeneratorBrowserPool:
    """Test browser pool management in PDF generator."""

    @pytest.mark.asyncio
    async def test_generate_pdf_success(self, mock_browser_pool):
        """Test successful PDF generation."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page, _ = _mock_page_and_response()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

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
        assert page.goto.call_args.kwargs.get("wait_until") == "networkidle"
        page.pdf.assert_called_once()
        context.close.assert_called_once()
        pool_instance.release_browser.assert_called_once_with(browser)

    @pytest.mark.asyncio
    async def test_generate_pdf_http_error(self, mock_browser_pool):
        """Test PDF generation with HTTP error."""
        pool_instance, browser = mock_browser_pool

        context = AsyncMock()
        page, _ = _mock_page_and_response(status=404, status_text="Not Found")
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

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
        page.url = "https://example.com"
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
        page, _ = _mock_page_and_response()
        browser.new_context = AsyncMock(return_value=context)
        context.new_page = AsyncMock(return_value=page)

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


@pytest.mark.unit
class TestGlobalPDFFunctions:
    """Test global PDF generation functions."""

    @pytest.mark.asyncio
    async def test_generate_pdf_from_url(self):
        """Test generate_pdf_from_url function."""
        with patch("downloader.pdf_generator.get_pdf_generator") as mock_get_generator:
            mock_generator = AsyncMock()
            mock_generator.generate_pdf = AsyncMock(return_value=b"PDF content")

            mock_context = AsyncMock()
            mock_context.__aenter__ = AsyncMock(return_value=mock_generator)
            mock_context.__aexit__ = AsyncMock(return_value=None)
            mock_get_generator.return_value = mock_context

            result = await generate_pdf_from_url("https://example.com")

            assert result == b"PDF content"
            mock_generator.generate_pdf.assert_called_once_with(
                "https://example.com",
                None,
                actions=None,
                wait_for_selector=None,
                max_output_bytes=None,
            )

    @pytest.mark.asyncio
    async def test_get_pdf_generator_creates_new(self):
        """Test that get_pdf_generator creates new instance when none exists."""
        import downloader.pdf_generator

        downloader.pdf_generator._pdf_generator = None

        with patch("downloader.pdf_generator.PlaywrightPDFGenerator") as mock_class:
            mock_instance = AsyncMock()
            mock_class.return_value = mock_instance

            async with get_pdf_generator() as generator:
                assert generator == mock_instance
                mock_instance.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_pdf_generator_reuses_existing(self):
        """Test that get_pdf_generator reuses existing instance."""
        import downloader.pdf_generator

        existing_instance = AsyncMock()
        downloader.pdf_generator._pdf_generator = existing_instance

        async with get_pdf_generator() as generator:
            assert generator == existing_instance
            existing_instance.start.assert_not_called()

"""Tests for global PDF generation functions."""

from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.pdf_generator import (
    cleanup_pdf_generator,
    generate_pdf_from_url,
    get_pdf_generator,
    get_shared_pdf_generator,
    set_shared_pdf_generator,
)


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

    @pytest.mark.asyncio
    async def test_shared_pdf_generator_registration_and_cleanup(self):
        """Test set_shared_pdf_generator unifies get_shared_pdf_generator and cleanup."""
        mock_generator = AsyncMock()
        set_shared_pdf_generator(mock_generator)
        assert get_shared_pdf_generator() is mock_generator

        async with get_pdf_generator() as yielded:
            assert yielded is mock_generator
            mock_generator.start.assert_not_called()

        await cleanup_pdf_generator()
        mock_generator.close.assert_called_once()
        assert get_shared_pdf_generator() is None

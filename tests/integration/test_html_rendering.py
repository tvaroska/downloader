"""Integration tests for HTML rendering with Playwright."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.downloader.content_converter import render_html_with_playwright
from src.downloader.services.content_processor import handle_html_response


@pytest.mark.integration
@pytest.mark.asyncio
class TestPlaywrightHTMLRendering:
    """Test Playwright HTML rendering functionality."""

    async def test_render_html_with_playwright_success(self, mock_playwright_for_html):
        """Test successful HTML rendering with Playwright."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mock page to return rendered HTML
        rendered_html = """<!DOCTYPE html>
<html>
<head>
    <meta property="og:title" content="Rendered Title">
    <meta property="og:description" content="Rendered Description">
</head>
<body><h1>Rendered Content</h1></body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        # Execute
        url = "https://example.com/article"
        result = await render_html_with_playwright(url)

        # Verify
        assert isinstance(result, bytes)
        assert b"Rendered Title" in result
        assert b"Rendered Description" in result
        assert b"Rendered Content" in result

        # Verify Playwright calls
        mock_page.goto.assert_called_once()
        assert url in str(mock_page.goto.call_args)
        mock_page.wait_for_load_state.assert_called()
        mock_page.content.assert_called_once()
        mock_context.close.assert_called_once()

    async def test_render_html_with_playwright_page_load_failure(self, mock_playwright_for_html):
        """Test handling of page load failures."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mock to return 404
        mock_page.goto.return_value = AsyncMock(status=404)

        # Execute and expect exception
        url = "https://example.com/not-found"
        with pytest.raises(Exception, match="Failed to load page"):
            await render_html_with_playwright(url)

        # Verify cleanup still happens
        mock_context.close.assert_called_once()

    async def test_render_html_with_playwright_timeout(self, mock_playwright_for_html):
        """Test handling of page load timeout."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mock to timeout
        import asyncio

        mock_page.goto.side_effect = asyncio.TimeoutError("Page load timeout")

        # Execute and expect exception
        url = "https://example.com/slow-page"
        with pytest.raises((Exception, asyncio.TimeoutError)):
            await render_html_with_playwright(url)

        # Verify cleanup still happens
        mock_context.close.assert_called_once()

    async def test_render_html_closes_modals(self, mock_playwright_for_html):
        """Test that modal closing logic is attempted."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mocks
        mock_page.goto.return_value = AsyncMock(status=200)
        mock_page.content.return_value = "<html><body>Content</body></html>"

        # Create mock close button
        mock_close_button = AsyncMock()
        mock_page.query_selector_all.return_value = [mock_close_button]

        # Execute
        url = "https://example.com/article"
        await render_html_with_playwright(url)

        # Verify modal closing was attempted
        assert mock_page.query_selector_all.called
        # Check that close button selectors were queried
        call_args = [str(call) for call in mock_page.query_selector_all.call_args_list]
        assert any("close" in str(arg).lower() for arg in call_args)

    async def test_render_html_browser_pool_release(self, mock_playwright_for_html):
        """Test that browser is properly released back to pool."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        mock_page.goto.return_value = AsyncMock(status=200)
        mock_page.content.return_value = "<html><body>Content</body></html>"

        # Execute
        url = "https://example.com/article"
        await render_html_with_playwright(url)

        # Verify browser was released
        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)

    async def test_render_html_browser_not_initialized(self):
        """Test error when browser pool is not initialized."""
        # Mock get_shared_pdf_generator to return None
        with patch("src.downloader.content_converter.get_shared_pdf_generator", return_value=None):
            url = "https://example.com/article"

            with pytest.raises(Exception, match="PDF generator pool not initialized"):
                await render_html_with_playwright(url)

    async def test_render_html_no_response_from_goto(self, mock_playwright_for_html):
        """Test handling when page.goto() returns None."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mock to return None
        mock_page.goto.return_value = None

        # Execute and expect exception
        url = "https://example.com/article"
        with pytest.raises(Exception, match="Failed to load page"):
            await render_html_with_playwright(url)


@pytest.mark.integration
@pytest.mark.asyncio
class TestHandleHTMLResponse:
    """Test the handle_html_response function integration."""

    async def test_handle_html_response_triggers_playwright_for_substack(
        self, mock_playwright_for_html, substack_minimal_html
    ):
        """Test that Substack URLs trigger Playwright rendering."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mocks
        rendered_html = """<!DOCTYPE html>
<html>
<head>
    <meta property="og:title" content="Rendered Substack">
    <meta property="og:description" content="Complete content">
</head>
<body><article>Full content here</article></body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        # Clear caches
        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Prepare input
        url = "https://example.substack.com/p/test-article"
        content = substack_minimal_html
        metadata = {
            "url": url,
            "content_type": "text/html; charset=utf-8",
            "size": len(content),
            "status_code": 200,
            "headers": {},
        }

        # Execute
        response = await handle_html_response(url, content, metadata)

        # Verify response
        assert response.status_code == 200
        assert b"Rendered Substack" in response.body
        assert b"Complete content" in response.body

        # Verify header indicates JS rendering
        assert response.headers.get("X-Rendered-With-JS") == "true"

        # Verify Playwright was called
        mock_page.goto.assert_called_once()

    async def test_handle_html_response_skips_playwright_for_static(self, static_html_complete):
        """Test that static HTML with metadata skips Playwright rendering."""
        # Clear caches
        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Prepare input
        url = "https://example.com/blog/static-post"
        content = static_html_complete
        metadata = {
            "url": url,
            "content_type": "text/html; charset=utf-8",
            "size": len(content),
            "status_code": 200,
            "headers": {},
        }

        # Execute
        response = await handle_html_response(url, content, metadata)

        # Verify response uses raw HTML
        assert response.status_code == 200
        assert b"Static Blog Post" in response.body

        # Verify header indicates NO JS rendering
        assert response.headers.get("X-Rendered-With-JS") == "false"

    async def test_handle_html_response_graceful_degradation_on_playwright_failure(
        self, mock_playwright_for_html, substack_minimal_html
    ):
        """Test graceful degradation when Playwright rendering fails."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_html

        # Configure mock to fail
        mock_page.goto.side_effect = Exception("Playwright failed")

        # Clear caches
        from src.downloader import content_converter

        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        # Prepare input
        url = "https://example.substack.com/p/test-article"
        content = substack_minimal_html
        metadata = {
            "url": url,
            "content_type": "text/html; charset=utf-8",
            "size": len(content),
            "status_code": 200,
            "headers": {},
        }

        # Execute - should NOT raise exception (graceful degradation)
        response = await handle_html_response(url, content, metadata)

        # Verify response uses raw HTML (fallback)
        assert response.status_code == 200
        assert response.headers.get("X-Rendered-With-JS") == "false"
        # Should return original minimal HTML
        assert b"Loading..." in response.body or b"root" in response.body


# Fixtures for mocking Playwright components
@pytest.fixture
def mock_playwright_for_html():
    """Mock Playwright components for HTML rendering tests."""
    # Create mock objects
    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_browser = MagicMock()
    mock_pool = AsyncMock()
    mock_generator = MagicMock()

    # Configure mock behavior
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.content = AsyncMock()
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.wait_for_timeout = AsyncMock()

    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_browser.new_context = AsyncMock(return_value=mock_context)

    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.release_browser = AsyncMock()

    mock_generator.pool = mock_pool

    # Patch get_shared_pdf_generator
    with patch(
        "src.downloader.content_converter.get_shared_pdf_generator",
        return_value=mock_generator,
    ):
        yield mock_generator, mock_browser, mock_context, mock_page

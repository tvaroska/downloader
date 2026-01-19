"""Integration tests for browser rendering with Playwright - ?render=true and ?wait_for= parameters."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.downloader.content_converter import (
    SelectorTimeoutError,
    render_html_with_playwright,
)


@pytest.fixture
def mock_playwright_for_rendering():
    """Mock Playwright components for browser rendering tests."""
    mock_page = AsyncMock()
    mock_context = AsyncMock()
    mock_browser = MagicMock()
    mock_pool = AsyncMock()
    mock_generator = MagicMock()

    # Configure mock behavior
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.content = AsyncMock()
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.wait_for_timeout = AsyncMock()

    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_pool.get_browser = AsyncMock(return_value=mock_browser)
    mock_pool.release_browser = AsyncMock()
    mock_pool.create_context = AsyncMock(return_value=mock_context)

    mock_generator.pool = mock_pool

    with patch(
        "src.downloader.content_converter.get_shared_pdf_generator",
        return_value=mock_generator,
    ):
        yield mock_generator, mock_browser, mock_context, mock_page


@pytest.mark.integration
@pytest.mark.asyncio
class TestBrowserRenderingJSContent:
    """Test browser rendering for JavaScript-heavy content."""

    async def test_render_react_hello_world_success(self, mock_playwright_for_rendering):
        """Test rendering React app - returns populated #root div content."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        rendered_html = """<!DOCTYPE html>
<html>
<head>
    <meta property="og:title" content="React App">
</head>
<body>
    <div id="root">
        <h1>Hello from React!</h1>
        <p>This content was rendered by JavaScript.</p>
    </div>
</body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        result = await render_html_with_playwright("https://example.com/react-app")

        assert isinstance(result, bytes)
        assert b"Hello from React!" in result
        assert b'<div id="root">' in result
        mock_page.goto.assert_called_once()
        mock_page.wait_for_load_state.assert_called()
        mock_context.close.assert_called_once()
        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)

    async def test_render_vue_hello_world_success(self, mock_playwright_for_rendering):
        """Test rendering Vue app - returns populated #app div content."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        rendered_html = """<!DOCTYPE html>
<html>
<body>
    <div id="app">
        <h1>Hello from Vue!</h1>
        <p>Vue.js rendered this content.</p>
    </div>
</body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        result = await render_html_with_playwright("https://example.com/vue-app")

        assert isinstance(result, bytes)
        assert b"Hello from Vue!" in result
        assert b'<div id="app">' in result

    async def test_render_angular_success(self, mock_playwright_for_rendering):
        """Test rendering Angular app with ng-app populated."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        rendered_html = """<!DOCTYPE html>
<html ng-app="myApp">
<body>
    <div ng-controller="MainController">
        <h1>Hello from Angular!</h1>
    </div>
</body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        result = await render_html_with_playwright("https://example.com/angular-app")

        assert b"Hello from Angular!" in result
        assert b'ng-app="myApp"' in result

    async def test_render_returns_utf8_bytes(self, mock_playwright_for_rendering):
        """Test that render result is properly UTF-8 encoded bytes."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        # HTML with unicode characters
        rendered_html = """<!DOCTYPE html>
<html>
<body><h1>Caf\u00e9 \u2603 \u4e2d\u6587</h1></body>
</html>"""
        mock_page.content.return_value = rendered_html
        mock_page.goto.return_value = AsyncMock(status=200)

        result = await render_html_with_playwright("https://example.com/unicode")

        assert isinstance(result, bytes)
        # Verify it can be decoded as UTF-8
        decoded = result.decode("utf-8")
        assert "Caf\u00e9" in decoded
        assert "\u2603" in decoded  # Snowman
        assert "\u4e2d\u6587" in decoded  # Chinese characters

    async def test_render_navigates_with_networkidle(self, mock_playwright_for_rendering):
        """Test that page.goto is called with wait_until='networkidle'."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = "<html><body>Content</body></html>"
        mock_page.goto.return_value = AsyncMock(status=200)

        await render_html_with_playwright("https://example.com/test")

        mock_page.goto.assert_called_once()
        call_kwargs = mock_page.goto.call_args[1]
        assert call_kwargs.get("wait_until") == "networkidle"
        assert call_kwargs.get("timeout") == 10000


@pytest.mark.integration
@pytest.mark.asyncio
class TestWaitForSelectorFunctionality:
    """Test wait_for CSS selector functionality."""

    async def test_wait_for_selector_found_success(self, mock_playwright_for_rendering):
        """Test that wait_for_selector succeeds when element is found."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = (
            "<html><body><div class='article'>Content</div></body></html>"
        )
        mock_page.goto.return_value = AsyncMock(status=200)

        result = await render_html_with_playwright(
            "https://example.com/article", wait_for_selector=".article"
        )

        assert b"article" in result
        mock_page.wait_for_selector.assert_called_once()
        call_args, call_kwargs = mock_page.wait_for_selector.call_args
        assert call_args[0] == ".article"

    async def test_wait_for_selector_with_complex_css(self, mock_playwright_for_rendering):
        """Test complex CSS selector like div.article > h1.title."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = "<html><body>Content</body></html>"
        mock_page.goto.return_value = AsyncMock(status=200)

        complex_selector = "div.article > h1.title"
        await render_html_with_playwright(
            "https://example.com/article", wait_for_selector=complex_selector
        )

        call_args, _ = mock_page.wait_for_selector.call_args
        assert call_args[0] == complex_selector

    async def test_wait_for_selector_visible_state(self, mock_playwright_for_rendering):
        """Test that wait_for_selector uses state='visible'."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = "<html><body>Content</body></html>"
        mock_page.goto.return_value = AsyncMock(status=200)

        await render_html_with_playwright(
            "https://example.com/article", wait_for_selector="#content"
        )

        call_kwargs = mock_page.wait_for_selector.call_args[1]
        assert call_kwargs.get("state") == "visible"

    async def test_wait_for_selector_timeout_10000ms(self, mock_playwright_for_rendering):
        """Test that wait_for_selector uses 10000ms timeout."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = "<html><body>Content</body></html>"
        mock_page.goto.return_value = AsyncMock(status=200)

        await render_html_with_playwright(
            "https://example.com/article", wait_for_selector=".content"
        )

        call_kwargs = mock_page.wait_for_selector.call_args[1]
        assert call_kwargs.get("timeout") == 10000

    async def test_wait_for_selector_timeout_raises_error(self, mock_playwright_for_rendering):
        """Test that PlaywrightTimeoutError is converted to SelectorTimeoutError."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = AsyncMock(status=200)
        mock_page.wait_for_selector.side_effect = PlaywrightTimeoutError(
            "Timeout waiting for selector"
        )

        with pytest.raises(SelectorTimeoutError) as exc_info:
            await render_html_with_playwright(
                "https://example.com/article", wait_for_selector=".nonexistent"
            )

        assert exc_info.value.selector == ".nonexistent"
        assert exc_info.value.timeout_ms == 10000
        assert ".nonexistent" in str(exc_info.value)
        assert "10000ms" in str(exc_info.value)

    async def test_wait_for_selector_cleanup_on_timeout(self, mock_playwright_for_rendering):
        """Test that cleanup happens even when selector times out."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = AsyncMock(status=200)
        mock_page.wait_for_selector.side_effect = PlaywrightTimeoutError("Timeout")

        with pytest.raises(SelectorTimeoutError):
            await render_html_with_playwright(
                "https://example.com/article", wait_for_selector=".missing"
            )

        # Verify cleanup still occurs
        mock_context.close.assert_called_once()
        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)


@pytest.mark.integration
@pytest.mark.asyncio
class TestBrowserRenderingTimeouts:
    """Test timeout handling for browser rendering."""

    async def test_page_navigation_timeout(self, mock_playwright_for_rendering):
        """Test handling of page navigation timeout."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.side_effect = asyncio.TimeoutError("Page load timeout")

        with pytest.raises((asyncio.TimeoutError, Exception)):
            await render_html_with_playwright("https://example.com/slow-page")

        # Verify cleanup still happens
        mock_context.close.assert_called_once()

    async def test_networkidle_timeout(self, mock_playwright_for_rendering):
        """Test handling of network idle timeout."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = AsyncMock(status=200)
        mock_page.wait_for_load_state.side_effect = asyncio.TimeoutError("Network idle timeout")

        with pytest.raises((asyncio.TimeoutError, Exception)):
            await render_html_with_playwright("https://example.com/ajax-heavy")

        mock_context.close.assert_called_once()

    async def test_cleanup_on_any_exception(self, mock_playwright_for_rendering):
        """Test that browser resources are cleaned up on any exception."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.side_effect = Exception("Unexpected error")

        with pytest.raises(Exception, match="Unexpected error"):
            await render_html_with_playwright("https://example.com/error")

        # Both context close and browser release should be called
        mock_context.close.assert_called_once()
        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)


@pytest.mark.integration
@pytest.mark.asyncio
class TestBrowserRenderingErrorHandling:
    """Test error handling for network and browser failures."""

    async def test_network_error_connection_refused(self, mock_playwright_for_rendering):
        """Test handling of network connection errors."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.side_effect = Exception("net::ERR_CONNECTION_REFUSED")

        with pytest.raises(Exception, match="ERR_CONNECTION_REFUSED"):
            await render_html_with_playwright("https://unreachable.example.com")

        mock_generator.pool.release_browser.assert_called_once()

    async def test_page_load_returns_404(self, mock_playwright_for_rendering):
        """Test handling of HTTP 404 response."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = AsyncMock(status=404)

        with pytest.raises(Exception, match="Failed to load page"):
            await render_html_with_playwright("https://example.com/not-found")

    async def test_page_load_returns_500(self, mock_playwright_for_rendering):
        """Test handling of HTTP 500 response."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = AsyncMock(status=500)

        with pytest.raises(Exception, match="Failed to load page"):
            await render_html_with_playwright("https://example.com/server-error")

    async def test_page_goto_returns_none(self, mock_playwright_for_rendering):
        """Test handling when page.goto returns None."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.return_value = None

        with pytest.raises(Exception, match="Failed to load page"):
            await render_html_with_playwright("https://example.com/no-response")

    async def test_browser_pool_not_initialized(self):
        """Test error when browser pool is not initialized."""
        with patch("src.downloader.content_converter.get_shared_pdf_generator", return_value=None):
            with pytest.raises(Exception, match="PDF generator pool not initialized"):
                await render_html_with_playwright("https://example.com/article")

    async def test_browser_released_after_successful_render(self, mock_playwright_for_rendering):
        """Test that browser is properly released back to pool after success."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.content.return_value = "<html><body>Success</body></html>"
        mock_page.goto.return_value = AsyncMock(status=200)

        await render_html_with_playwright("https://example.com/success")

        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)

    async def test_browser_released_after_failed_render(self, mock_playwright_for_rendering):
        """Test that browser is released back to pool even after failure."""
        mock_generator, mock_browser, mock_context, mock_page = mock_playwright_for_rendering

        mock_page.goto.side_effect = Exception("Render failed")

        with pytest.raises(Exception, match="Render failed"):
            await render_html_with_playwright("https://example.com/fail")

        mock_generator.pool.release_browser.assert_called_once_with(mock_browser)

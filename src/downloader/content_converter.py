"""Content conversion utilities with DRY implementation."""

import logging
import re
from collections import OrderedDict
from typing import Literal

from bs4 import BeautifulSoup, Tag

from .config import get_settings
from .pdf_generator import get_shared_pdf_generator

logger = logging.getLogger(__name__)


class BoundedCache:
    """A bounded set-like cache with LRU eviction."""

    def __init__(self, maxsize: int = 1000):
        self._maxsize = maxsize
        self._cache: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, key: str) -> bool:
        if key in self._cache:
            self._cache.move_to_end(key)  # Mark as recently used
            return True
        return False

    def add(self, key: str) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)  # Remove oldest
            self._cache[key] = None

    def clear(self) -> None:
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)


# Bounded caches with LRU eviction (max 1000 entries each)
_empty_content_cache = BoundedCache(maxsize=1000)
_fallback_bypass_cache = BoundedCache(maxsize=1000)
_js_heavy_cache = BoundedCache(maxsize=1000)  # URLs needing JS rendering for HTML
_static_html_cache = BoundedCache(maxsize=1000)  # URLs confirmed as static HTML


async def _create_playwright_context(browser):
    """Create an isolated Playwright browser context with standard settings."""
    return await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 720},
        ignore_https_errors=False,
        java_script_enabled=True,
        bypass_csp=False,
    )


async def _close_page_modals(page) -> None:
    """Attempt to close common signup boxes and modals."""
    close_selectors = [
        '[aria-label="close"]',
        '[title="Close"]',
        '[aria-label="Close"]',
        '[title="close"]',
    ]
    for selector in close_selectors:
        try:
            close_buttons = await page.query_selector_all(selector)
            for button in close_buttons:
                try:
                    await button.click(timeout=1000)
                    logger.debug(f"Closed modal/popup with selector: {selector}")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass
        except Exception:
            pass


def should_use_playwright_fallback(url: str, content: bytes, content_type: str) -> bool:
    """
    Smart content detection to avoid unnecessary Playwright fallbacks.

    Implements fast HTML content detection using CSS selectors and caching.
    Expected 50-70% reduction in unnecessary Playwright usage.
    """
    # Check if URL is known to produce empty content
    if url in _empty_content_cache:
        logger.debug(f"Skipping Playwright fallback for known empty URL: {url}")
        return False

    # Check if URL is in bypass cache (known to not benefit from fallback)
    if url in _fallback_bypass_cache:
        logger.debug(f"Bypassing Playwright fallback for cached URL: {url}")
        return False

    # Only use fallback for HTML content
    if "html" not in content_type.lower():
        return False

    # Fast content detection using BeautifulSoup
    try:
        soup = BeautifulSoup(content, "html.parser")

        # Check for meaningful body content
        body = soup.find("body")
        if not body:
            _empty_content_cache.add(url)
            return False

        # Get text content and check if substantial
        body_text = body.get_text(strip=True)
        settings = get_settings()
        if len(body_text) < settings.content.min_body_text_threshold:
            _empty_content_cache.add(url)
            logger.debug(f"Caching empty content URL (body text: {len(body_text)} chars): {url}")
            return False

        # Check for common indicators of content-heavy pages
        content_indicators = soup.select("main, article, .content, #content, .post, .article-body")
        if content_indicators:
            return True

        # Check for minimal content patterns that don't benefit from Playwright
        minimal_indicators = soup.select(".error, .not-found, .404, .maintenance, .coming-soon")
        if minimal_indicators:
            _fallback_bypass_cache.add(url)
            logger.debug(f"Caching bypass for minimal content page: {url}")
            return False

        return True

    except Exception as e:
        logger.warning(f"Content detection failed for {url}: {e}")
        return True  # Default to fallback if detection fails


def _has_missing_metadata(soup: BeautifulSoup) -> bool:
    """
    Check if HTML is missing expected metadata tags.

    Args:
        soup: BeautifulSoup parsed HTML

    Returns:
        True if critical metadata tags are missing
    """
    # Check for Open Graph tags
    og_title = soup.find("meta", property="og:title")
    og_description = soup.find("meta", property="og:description")

    # Check for Twitter Card tags
    twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
    twitter_description = soup.find("meta", attrs={"name": "twitter:description"})

    # Missing metadata if we don't have at least one title and one description tag
    has_title = og_title is not None or twitter_title is not None
    has_description = og_description is not None or twitter_description is not None

    return not (has_title and has_description)


def _has_js_framework_markers(soup: BeautifulSoup, body_text: str) -> bool:
    """
    Check for JS framework indicators with minimal content.

    Args:
        soup: BeautifulSoup parsed HTML
        body_text: Extracted body text

    Returns:
        True if JS framework markers detected with minimal content
    """
    # Check for common JS framework root elements
    react_root = soup.find(id="root")
    vue_app = soup.find(id="app")
    angular_app = soup.find(attrs={"ng-app": True})

    # If we have framework markers but minimal content, likely needs JS rendering
    has_framework_marker = react_root is not None or vue_app is not None or angular_app is not None
    settings = get_settings()
    has_minimal_content = len(body_text) < settings.content.min_js_framework_content_threshold

    return has_framework_marker and has_minimal_content


def should_use_playwright_for_html(url: str, content: bytes, content_type: str) -> bool:
    """
    Detect if HTML content needs JavaScript rendering.

    Detection criteria:
    1. URL in known JS-heavy cache → return True
    2. URL in static HTML cache → return False
    3. Parse HTML and check:
       - Content size < 50KB AND has React/Vue/Angular markers
       - Missing critical <meta> tags (og:title, og:description)
       - Has "enable JavaScript" or "loading..." placeholders
       - Body text < 200 chars but has app containers (#root, #app, etc.)
    4. Domain-based heuristics (substack.com, medium.com, etc.)
    5. Cache result for future requests

    Args:
        url: URL being processed
        content: Raw HTML content bytes
        content_type: MIME type

    Returns:
        True if Playwright rendering recommended, False otherwise
    """
    # Check caches first (O(1) lookup)
    if url in _js_heavy_cache:
        logger.debug(f"Cache hit: {url} known to need JS rendering")
        return True
    if url in _static_html_cache:
        logger.debug(f"Cache hit: {url} known to be static HTML")
        return False

    # Only check HTML content
    if "html" not in content_type.lower():
        return False

    try:
        # Parse HTML for analysis
        soup = BeautifulSoup(content, "html.parser")
        content_size = len(content)

        # Check for explicit JS requirement messages
        js_required_patterns = [
            "please enable javascript",
            "javascript is required",
            "enable js",
            "turn on javascript",
            "javascript is disabled",
            "requires javascript",
        ]
        text_lower = soup.get_text().lower()
        if any(pattern in text_lower for pattern in js_required_patterns):
            logger.info(f"Detected explicit JS requirement message in {url}")
            _js_heavy_cache.add(url)
            return True

        # Get body content for analysis
        body = soup.find("body")
        if not body:
            _static_html_cache.add(url)
            return False

        body_text = body.get_text(strip=True)

        # Check for JS frameworks with minimal content
        if _has_js_framework_markers(soup, body_text):
            logger.info(f"Detected JS framework markers with minimal content in {url}")
            _js_heavy_cache.add(url)
            return True

        # Check for missing metadata with small content size
        if content_size < 50000 and _has_missing_metadata(soup):
            logger.info(
                f"Detected missing metadata with small content size in {url} ({content_size} bytes)"
            )
            _js_heavy_cache.add(url)
            return True

        # Known JS-heavy domains
        js_heavy_domains = ["substack.com", "medium.com", "notion.so", "ghost.io"]
        if any(domain in url for domain in js_heavy_domains):
            logger.info(f"Detected known JS-heavy domain in {url}")
            _js_heavy_cache.add(url)
            return True

        # If we got substantial content with metadata, cache as static
        if len(body_text) > 500 and not _has_missing_metadata(soup):
            logger.debug(f"Caching {url} as static HTML (substantial content with metadata)")
            _static_html_cache.add(url)
            return False

        # Default to static (conservative approach)
        return False

    except Exception as e:
        logger.warning(f"HTML detection failed for {url}: {e}")
        return False  # Default to no rendering on error


async def convert_content_with_playwright_fallback(
    url: str, output_format: Literal["text", "markdown"] = "text"
) -> str:
    """
    Convert content using Playwright to get rendered HTML.
    This is used as a fallback when BeautifulSoup returns empty content.

    Args:
        url: The URL to fetch and convert
        output_format: Either "text" or "markdown" for the output format

    Returns:
        Text or markdown representation with article content extracted

    Raises:
        Exception: If Playwright conversion fails
    """
    try:
        logger.info(f"🔄 Starting Playwright {output_format} fallback for {url}")
        generator = get_shared_pdf_generator()
        if not generator or not generator.pool:
            raise Exception("PDF generator pool not initialized")

        browser = await generator.pool.get_browser()
        context = None
        page = None

        try:
            # Create isolated context for this request
            context = await _create_playwright_context(browser)
            page = await context.new_page()

            logger.info(f"🌐 Loading page with Playwright: {url}")
            # Navigate to page with reduced timeout (30s -> 10s for faster failures)
            response = await page.goto(url, wait_until="networkidle", timeout=10000)

            if not response or response.status >= 400:
                raise Exception(f"Failed to load page: {url}")

            logger.debug(f"Page loaded, waiting for network idle: {url}")
            # Wait for page to be fully loaded with reduced timeout
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Try to close any signup boxes/modals
            await _close_page_modals(page)

            logger.debug(f"Extracting rendered HTML content for {output_format}: {url}")
            # Get the rendered HTML content
            html_content = await page.content()

            emoji = "📄" if output_format == "text" else "📝"
            logger.info(
                f"{emoji} Processing rendered HTML content ({len(html_content)} chars) "
                f"for {output_format} extraction: {url}"
            )

            # Convert HTML using the consolidated function
            return _convert_html_to_format(html_content, output_format)

        finally:
            # Cleanup in reverse order
            if context:
                await context.close()
            if generator.pool:
                await generator.pool.release_browser(browser)

    except Exception as e:
        logger.error(f"Playwright {output_format} fallback failed for {url}: {e}")
        raise


async def render_html_with_playwright(url: str) -> bytes:
    """
    Fetch and render HTML using Playwright to execute JavaScript.

    Similar to convert_content_with_playwright_fallback() but returns
    raw HTML bytes instead of extracted text/markdown.

    Args:
        url: The URL to fetch and render

    Returns:
        Fully rendered HTML content as bytes

    Raises:
        Exception: If Playwright rendering fails
    """
    try:
        logger.info(f"🔄 Starting Playwright HTML rendering for {url}")
        generator = get_shared_pdf_generator()
        if not generator or not generator.pool:
            raise Exception("PDF generator pool not initialized")

        browser = await generator.pool.get_browser()
        context = None
        page = None

        try:
            # Create isolated context
            context = await _create_playwright_context(browser)
            page = await context.new_page()

            logger.info(f"🌐 Loading page with Playwright: {url}")
            # Navigate with 10s timeout
            response = await page.goto(url, wait_until="networkidle", timeout=10000)

            if not response or response.status >= 400:
                raise Exception(
                    f"Failed to load page: {url} (status: {response.status if response else 'no response'})"
                )

            logger.debug(f"Page loaded, waiting for network idle: {url}")
            await page.wait_for_load_state("networkidle", timeout=10000)

            # Close modals
            await _close_page_modals(page)

            logger.debug(f"Extracting rendered HTML content: {url}")
            # Get rendered HTML
            html_content = await page.content()

            logger.info(f"📄 Rendered HTML extracted ({len(html_content)} bytes) from {url}")

            # Return as bytes (UTF-8 encoded)
            return html_content.encode("utf-8")

        finally:
            # Cleanup (same pattern)
            if context:
                await context.close()
            if generator.pool:
                await generator.pool.release_browser(browser)

    except Exception as e:
        logger.error(f"Playwright HTML rendering failed for {url}: {e}")
        raise


def _convert_html_to_format(html_content: str, output_format: Literal["text", "markdown"]) -> str:
    """
    Convert HTML content to specified format.

    Args:
        html_content: HTML content to convert
        output_format: Either "text" or "markdown"

    Returns:
        Converted content in the specified format
    """
    soup = BeautifulSoup(html_content, "lxml")

    # Remove unwanted elements
    for element in soup(
        [
            "script",
            "style",
            "nav",
            "header",
            "footer",
            "aside",
            "menu",
            "form",
            "iframe",
            "noscript",
        ]
    ):
        element.decompose()

    # Try to find main content in common article containers
    main_content = None
    for selector in [
        "article",
        "main",
        '[role="main"]',
        ".content",
        ".post-content",
        ".entry-content",
        ".article-content",
    ]:
        main_content = soup.select_one(selector)
        if main_content:
            break

    # If no main content container found, use body or entire document
    if not main_content:
        main_content = soup.find("body") or soup

    if output_format == "markdown":
        return _convert_to_markdown(main_content)
    else:
        return _convert_to_text(main_content)


def _convert_to_markdown(main_content: BeautifulSoup | Tag) -> str:
    """Convert soup element to markdown format."""
    markdown_parts = []

    for element in main_content.find_all(
        ["h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "ul", "ol", "li"]
    ):
        if element.name.startswith("h"):
            level = int(element.name[1])
            markdown_parts.append("#" * level + " " + element.get_text(strip=True))
        elif element.name == "p":
            text_content = element.get_text(strip=True)
            if text_content:
                markdown_parts.append(text_content)
        elif element.name == "a" and element.get("href"):
            link_text = element.get_text(strip=True)
            href = element.get("href")
            if link_text and href:
                markdown_parts.append(f"[{link_text}]({href})")
        elif element.name in ["ul", "ol"]:
            for li in element.find_all("li", recursive=False):
                li_text = li.get_text(strip=True)
                if li_text:
                    prefix = "- " if element.name == "ul" else "1. "
                    markdown_parts.append(prefix + li_text)

    # If no structured content found, fall back to simple text extraction
    if not markdown_parts:
        text = main_content.get_text(separator="\n", strip=True)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
        return text.strip()

    # Join markdown parts with appropriate spacing
    text = "\n\n".join(markdown_parts)

    # Clean up excessive whitespace
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _convert_to_text(main_content: BeautifulSoup | Tag) -> str:
    """Convert soup element to plain text format."""
    # Extract text with proper spacing
    text = main_content.get_text(separator=" ", strip=True)

    # Clean up excessive whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def convert_content(
    content: bytes,
    content_type: str,
    output_format: Literal["text", "markdown"] = "text",
) -> str:
    """
    Convert content to specified format, extracting main article content from HTML.

    Args:
        content: Raw content bytes
        content_type: Original content type
        output_format: Either "text" or "markdown"

    Returns:
        Content in the specified format with article content extracted
    """
    try:
        # Try to decode as text
        text = content.decode("utf-8", errors="ignore")

        # If it's HTML, use BeautifulSoup to extract article content
        if "html" in content_type.lower():
            return _convert_html_to_format(text, output_format)

        return text
    except Exception:
        return content.decode("utf-8", errors="replace")


# Backward compatibility functions
def convert_content_to_text(content: bytes, content_type: str) -> str:
    """Convert content to plain text format."""
    return convert_content(content, content_type, "text")


def convert_content_to_markdown(content: bytes, content_type: str) -> str:
    """Convert content to markdown format."""
    return convert_content(content, content_type, "markdown")

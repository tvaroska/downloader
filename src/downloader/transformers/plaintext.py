"""HTML to plain text transformer using BeautifulSoup."""

import logging
import re

from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)


def html_to_plaintext(
    html: str | bytes,
    *,
    strip_tags: list[str] | None = None,
    extract_main_content: bool = True,
    separator: str = " ",
    preserve_paragraphs: bool = False,
) -> str:
    """
    Convert HTML to plain text format.

    Args:
        html: HTML content as string or bytes
        strip_tags: Additional tags to strip (script/style always stripped)
        extract_main_content: Whether to extract article/main content first
        separator: Character to use between text nodes (default: space)
        preserve_paragraphs: If True, preserve paragraph breaks with double newlines

    Returns:
        Plain text string with HTML tags stripped
    """
    # Handle bytes input
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")

    # Parse HTML
    soup = BeautifulSoup(html, "lxml")

    # Always strip non-content elements
    default_strip = ["script", "style", "nav", "header", "footer", "aside", "menu", "form"]
    all_strip = list(set(default_strip + (strip_tags or [])))

    for tag_name in all_strip:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # Extract main content if requested
    content_source: BeautifulSoup | Tag = soup
    if extract_main_content:
        main_content: Tag | None = None
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
        if main_content:
            content_source = main_content
        else:
            body = soup.find("body")
            if body and isinstance(body, Tag):
                content_source = body

    # Extract text
    if preserve_paragraphs:
        # Add newlines after block elements for paragraph preservation
        for br in content_source.find_all("br"):
            br.replace_with("\n")
        for block in content_source.find_all(
            ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"]
        ):
            block.insert_after("\n\n")
        text = content_source.get_text(separator=" ", strip=True)
        # Normalize to double newlines for paragraphs
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    else:
        text = content_source.get_text(separator=separator, strip=True)
        # Clean up excessive whitespace
        text = re.sub(r"\s+", " ", text)

    return text.strip()

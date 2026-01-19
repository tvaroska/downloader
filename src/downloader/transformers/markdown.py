"""HTML to Markdown transformer using markdownify."""

import logging
import re
from typing import Literal

from bs4 import BeautifulSoup, Tag
from markdownify import ATX, UNDERLINED, MarkdownConverter

logger = logging.getLogger(__name__)


class ContentMarkdownConverter(MarkdownConverter):
    """Custom markdown converter with enhanced code block and structure handling."""

    def convert_pre(self, el: Tag, text: str, convert_as_inline: bool) -> str:
        """Handle pre tags with code language detection."""
        # Detect language from class attribute (e.g., class="language-python")
        code_el = el.find("code")
        if code_el and isinstance(code_el, Tag):
            classes = code_el.get("class")
            if isinstance(classes, list):
                for cls in classes:
                    if isinstance(cls, str) and cls.startswith("language-"):
                        lang = cls.replace("language-", "")
                        return f"\n```{lang}\n{text.strip()}\n```\n"
        return f"\n```\n{text.strip()}\n```\n"


def html_to_markdown(
    html: str | bytes,
    *,
    strip_tags: list[str] | None = None,
    heading_style: Literal["atx", "setext"] = "atx",
    bullets: str = "*",
    extract_main_content: bool = True,
) -> str:
    """
    Convert HTML to Markdown format.

    Args:
        html: HTML content as string or bytes
        strip_tags: Additional tags to strip (script/style always stripped)
        heading_style: "atx" (# headings) or "setext" (underlined)
        bullets: Bullet character for unordered lists
        extract_main_content: Whether to extract article/main content first

    Returns:
        Markdown formatted string
    """
    # Handle bytes input
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="ignore")

    # Parse HTML
    soup = BeautifulSoup(html, "lxml")

    # Always strip script, style, and other non-content elements
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

    # Configure converter
    heading_style_val = ATX if heading_style == "atx" else UNDERLINED

    # Convert to markdown using custom converter
    converter = ContentMarkdownConverter(
        heading_style=heading_style_val,
        bullets=bullets,
        strip=["script", "style"],  # Already removed, but belt-and-suspenders
    )

    markdown = converter.convert_soup(content_source)

    # Clean up whitespace
    markdown = re.sub(r"\n\s*\n\s*\n+", "\n\n", markdown)
    markdown = markdown.strip()

    return markdown

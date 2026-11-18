"""HTML to Markdown transformer using markdownify."""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup, Tag
from markdownify import ATX, MarkdownConverter

from .common import extract_main_content_node

logger = logging.getLogger(__name__)
_MULTI_NEWLINE_RE = re.compile(r"\n\s*\n\s*\n+")


class ContentMarkdownConverter(MarkdownConverter):
    """Custom markdown converter with fenced code block language preservation."""

    def convert_pre(self, el: Tag, text: str, **kwargs: Any) -> str:
        """Handle pre tags with code language detection."""
        _ = kwargs
        code_el = el.find("code")
        if isinstance(code_el, Tag):
            classes = code_el.get("class")
            if isinstance(classes, list):
                for cls in classes:
                    if isinstance(cls, str) and cls.startswith("language-"):
                        lang = cls.removeprefix("language-")
                        return f"\n```{lang}\n{text.strip()}\n```\n"
        return f"\n```\n{text.strip()}\n```\n"


def html_to_markdown(html: str | bytes | BeautifulSoup | Tag) -> str:
    """Convert HTML (or an already-extracted BeautifulSoup node) to Markdown."""
    content_source = extract_main_content_node(html)
    converter = ContentMarkdownConverter(
        heading_style=ATX,
        bullets="*",
        strip=["script", "style"],
    )
    markdown: str = converter.convert_soup(content_source)
    markdown = _MULTI_NEWLINE_RE.sub("\n\n", markdown)
    return markdown.strip()

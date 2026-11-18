"""HTML to plain text transformer using BeautifulSoup."""

from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup, Tag

from .common import extract_main_content_node

logger = logging.getLogger(__name__)
_WHITESPACE_RE = re.compile(r"\s+")


def html_to_plaintext(html: str | bytes | BeautifulSoup | Tag) -> str:
    """Convert HTML (or an already-extracted BeautifulSoup node) to normalized plain text."""
    content_source = extract_main_content_node(html)
    text = content_source.get_text(separator=" ", strip=True)
    return _WHITESPACE_RE.sub(" ", text).strip()

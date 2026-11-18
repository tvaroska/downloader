"""Shared HTML main-content extraction and tag-stripping helpers."""

from __future__ import annotations

from bs4 import BeautifulSoup, Tag

DEFAULT_STRIP_TAGS: tuple[str, ...] = (
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
)

MAIN_CONTENT_SELECTORS: tuple[str, ...] = (
    "article",
    "main",
    '[role="main"]',
    ".content",
    ".post-content",
    ".entry-content",
    ".article-content",
)


def extract_main_content_node(html: str | bytes | BeautifulSoup | Tag) -> BeautifulSoup | Tag:
    """Parse HTML once (unless already parsed), strip chrome tags, and return the primary node."""
    if isinstance(html, (BeautifulSoup, Tag)):
        return html

    decoded = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    soup = BeautifulSoup(decoded, "lxml")

    for tag in soup(list(DEFAULT_STRIP_TAGS)):
        tag.decompose()

    for selector in MAIN_CONTENT_SELECTORS:
        candidate = soup.select_one(selector)
        if candidate is not None:
            return candidate

    body = soup.find("body")
    if isinstance(body, Tag):
        return body
    return soup

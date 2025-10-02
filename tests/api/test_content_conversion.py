"""Tests for content conversion utility functions in the API module."""

import pytest

from src.downloader.content_converter import (
    convert_content_to_markdown,
    convert_content_to_text,
)
from src.downloader.services.content_processor import parse_accept_header


class TestContentConversion:
    @pytest.mark.parametrize(
        "header, expected_format",
        [
            ("text/plain", "text"),
            ("text/html", "html"),
            ("text/markdown", "markdown"),
            ("application/pdf", "pdf"),
            ("application/json", "json"),
            ("image/png", "raw"),
            ("", "text"),
            (None, "text"),
        ],
    )
    def test_parse_accept_header(self, header, expected_format):
        """Test parsing of Accept headers."""
        assert parse_accept_header(header) == expected_format

    def test_convert_content_to_text(self):
        """Test HTML to text conversion."""
        html = (
            b"<html><head><style>p {color: red;}</style></head>"
            b"<body><h1>Title</h1><p>Some text.</p></body></html>"
        )
        text = convert_content_to_text(html, "text/html")
        assert "Title" in text
        assert "Some text" in text
        assert "{color: red;}" not in text

    def test_convert_content_to_markdown(self):
        """Test HTML to markdown conversion."""
        html = b"<html><body><h1>Title</h1><p>A link: <a href='https://example.com'>Example</a></p></body></html>"
        markdown = convert_content_to_markdown(html, "text/html")
        assert "# Title" in markdown
        assert "[Example](https://example.com)" in markdown

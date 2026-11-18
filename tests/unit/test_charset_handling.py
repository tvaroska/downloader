"""Unit tests for non-UTF-8 charset decoding and UTF-8 browser HTML labeling (`R4`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.content_converter import convert_content_to_markdown, convert_content_to_text
from src.downloader.services.content_processor import handle_html_response
from src.downloader.services.multi_format import process_multiple_formats


@pytest.mark.unit
def test_text_and_markdown_respect_declared_charset() -> None:
    latin_html = (
        "<html><body><article><h1>Café crème</h1><p>Résumé naïve</p></article></body></html>"
    ).encode("iso-8859-1")
    text_out = convert_content_to_text(latin_html, "text/html; charset=iso-8859-1")
    md_out = convert_content_to_markdown(latin_html, "text/html; charset=iso-8859-1")
    assert "Café crème" in text_out
    assert "Résumé naïve" in md_out

    cyrillic_html = (
        "<html><head><meta charset='windows-1251'></head>"
        "<body><article><p>Привет мир</p></article></body></html>"
    ).encode("windows-1251")
    assert "Привет мир" in convert_content_to_text(cyrillic_html, "text/html")

    shift_jis_plain = "東京タワー".encode("shift_jis")
    assert convert_content_to_text(shift_jis_plain, "text/plain; charset=shift_jis") == "東京タワー"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_rendered_html_is_labelled_utf8() -> None:
    origin_latin = (
        b"<html><head><title>Stub</title></head><body><div id='root'></div></body></html>"
    )
    metadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=iso-8859-1"},
        "url": "https://example.com/spa",
        "size": len(origin_latin),
        "content_type": "text/html; charset=iso-8859-1",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    rendered_utf8 = "<html><body><p>Café crème — rendered</p></body></html>".encode()

    with patch(
        "src.downloader.services.content_processor.render_html_with_playwright",
        new=AsyncMock(return_value=rendered_utf8),
    ):
        resp = await handle_html_response(
            "https://example.com/spa",
            origin_latin,
            metadata,
            False,
            None,
            pdf_semaphore=asyncio.Semaphore(1),
        )

    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert resp.headers["x-original-content-type"] == "text/html; charset=iso-8859-1"
    assert resp.headers["x-rendered-with-js"] == "true"
    assert "Café crème — rendered" in bytes(resp.body).decode("utf-8")

    # Multi-format HTML + Markdown also preserves non-UTF-8 static characters without loss
    static_latin = (
        "<html><head><meta property='og:title' content='T'>"
        "<meta property='og:description' content='D'></head>"
        "<body><article><p>" + ("Café crème " * 60) + "</p></article></body></html>"
    ).encode("iso-8859-1")
    static_meta = {**metadata, "size": len(static_latin)}
    multi = await process_multiple_formats(
        "https://example.com/static-latin",
        static_latin,
        static_meta,
        ["html", "markdown"],
        asyncio.Semaphore(1),
    )
    assert "Café crème" in multi["text/html"]
    assert "Café crème" in multi["text/markdown"]

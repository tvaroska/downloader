"""Unit tests for non-UTF-8 charset decoding and UTF-8 browser HTML labeling (`R4`, `R78`)."""

from __future__ import annotations

import asyncio
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest

from downloader.browser.renderer import LocalBrowserRenderer
from downloader.content_converter import convert_content
from downloader.content_encoding import resolve_charset_and_decode
from downloader.models.responses import ResponseMetadata
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert
from downloader.services.multi_format import MultiFormatRequest, process_multiple_formats


@pytest.mark.unit
def test_text_and_markdown_respect_declared_charset() -> None:
    latin_html = (
        "<html><body><article><h1>Café crème</h1><p>Résumé naïve</p></article></body></html>"
    ).encode("iso-8859-1")
    text_out = convert_content(latin_html, "text/html; charset=iso-8859-1", "text")
    md_out = convert_content(latin_html, "text/html; charset=iso-8859-1", "markdown")
    assert "Café crème" in text_out
    assert "Résumé naïve" in md_out

    cyrillic_html = (
        "<html><head><meta charset='windows-1251'></head>"
        "<body><article><p>Привет мир</p></article></body></html>"
    ).encode("windows-1251")
    assert "Привет мир" in convert_content(cyrillic_html, "text/html", "text")

    shift_jis_plain = "東京タワー".encode("shift_jis")
    assert convert_content(shift_jis_plain, "text/plain; charset=shift_jis", "text") == "東京タワー"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_auto_rendered_html_is_labelled_utf8() -> None:
    origin_latin = (
        b"<html><head><title>Stub</title></head><body><div id='root'></div></body></html>"
    )
    metadata: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=iso-8859-1"},
        "url": "https://example.com/spa",
        "size": len(origin_latin),
        "content_type": "text/html; charset=iso-8859-1",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    rendered_utf8 = "<html><body><p>Café crème — rendered</p></body></html>".encode()
    deps = ConversionDeps(renderer=LocalBrowserRenderer(asyncio.Semaphore(1)))

    with patch(
        "downloader.browser.renderer.render_html_with_playwright",
        new=AsyncMock(return_value=rendered_utf8),
    ):
        resp = await convert(
            ConversionRequest(
                url="https://example.com/spa",
                output_format="html",
                content=origin_latin,
                metadata=metadata,
            ),
            deps,
        )

    assert resp.media_type == "text/html; charset=utf-8"
    assert resp.headers["X-Original-Content-Type"] == "text/html; charset=iso-8859-1"
    assert resp.headers["X-Rendered-With-JS"] == "true"
    assert "Café crème — rendered" in resp.body.decode("utf-8")

    # Multi-format HTML + Markdown also preserves non-UTF-8 static characters without loss
    static_latin = (
        "<html><head><meta property='og:title' content='T'>"
        "<meta property='og:description' content='D'></head>"
        "<body><article><p>" + ("Café crème " * 60) + "</p></article></body></html>"
    ).encode("iso-8859-1")
    static_meta: ResponseMetadata = {**metadata, "size": len(static_latin)}
    multi = await process_multiple_formats(
        MultiFormatRequest(
            url="https://example.com/static-latin",
            formats=["html", "markdown"],
            content=static_latin,
            metadata=static_meta,
        ),
        deps,
    )
    assert "Café crème" in multi["text/html"]
    assert "Café crème" in multi["text/markdown"]


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["text", "markdown"])
async def test_forced_render_text_ignores_legacy_meta_charset(
    output_format: Literal["text", "markdown"],
) -> None:
    """R78: UTF-8 Playwright DOM bytes with legacy `<meta charset='windows-1251'>` decode cleanly."""
    rendered_utf8 = (
        "<html><head><meta charset='windows-1251'></head>"
        "<body><article><h1>Заголовок</h1><p>Привет мир без кракозябр</p></article></body></html>"
    ).encode()
    deps = ConversionDeps(renderer=LocalBrowserRenderer(asyncio.Semaphore(1)))

    with patch(
        "downloader.browser.renderer.render_html_with_playwright",
        new=AsyncMock(return_value=rendered_utf8),
    ):
        res = await convert(
            ConversionRequest(
                url="https://example.com/legacy-cyrillic",
                output_format=output_format,
                force_render=True,
            ),
            deps,
        )

    assert res.rendered_with_js is True
    assert "Привет мир без кракозябр" in res.text
    assert "Ð" not in res.text

    # Also verify pre-rendered (already_rendered=True) path used by multi-format.
    pre_rendered_res = await convert(
        ConversionRequest(
            url="https://example.com/legacy-cyrillic",
            output_format=output_format,
            content=rendered_utf8,
            already_rendered=True,
        ),
        deps,
    )
    assert "Привет мир без кракозябр" in pre_rendered_res.text


@pytest.mark.unit
def test_meta_sniff_limited_to_markup_types() -> None:
    """R78: `<meta charset>` sniffing runs only for HTML/XML, and meta `utf-16*` maps to UTF-8."""
    plain_utf8 = '<meta charset="windows-1251"> Привет мир'.encode()
    decoded_plain, enc_plain = resolve_charset_and_decode(plain_utf8, "text/plain")
    assert enc_plain == "utf-8"
    assert "Привет мир" in decoded_plain

    json_utf8 = '{"snippet": "<meta charset=windows-1251> Привет"}'.encode()
    decoded_json, enc_json = resolve_charset_and_decode(json_utf8, "application/json")
    assert enc_json == "utf-8"
    assert "Привет" in decoded_json

    html_meta_utf16 = (
        b"<html><head><meta charset='utf-16'></head><body><p>Hello UTF-8</p></body></html>"
    )
    decoded_html, enc_html = resolve_charset_and_decode(html_meta_utf16, "text/html")
    assert enc_html == "utf-8"
    assert "Hello UTF-8" in decoded_html


@pytest.mark.unit
@pytest.mark.asyncio
async def test_content_encoding_edge_cases_and_content_negotiation() -> None:
    """Cover BOM/XML/meta decoding, header extraction, deflate/error streams, and IDNA header sanitization (`R80`)."""
    import zlib

    import httpx

    from downloader.content_encoding import (
        extract_declared_charset,
        extract_header_case_insensitive,
        is_text_convertible_content_type,
        make_stateless_cookies,
        read_bounded_response_bytes,
    )
    from downloader.services.content_negotiation import (
        format_to_mime_type,
        parse_accept_header,
        parse_accept_headers,
        sanitize_original_url_header,
    )

    # Stateless cookie jar rejects storage via extract_cookies and replay via set_cookie_header
    cookies = make_stateless_cookies()
    req = httpx.Request("GET", "https://example.com/")
    resp = httpx.Response(200, headers={"Set-Cookie": "session=abc; Path=/"}, request=req)
    cookies.extract_cookies(resp)
    assert len(cookies) == 0
    cookies.set("manual", "1", domain="example.com")
    out_req = httpx.Request("GET", "https://example.com/")
    cookies.set_cookie_header(out_req)
    assert "cookie" not in {k.lower() for k in out_req.headers}

    # Text-convertibility & NUL byte detection
    assert is_text_convertible_content_type("image/png", allow_ocr_images=True) is True
    assert is_text_convertible_content_type("", sample_bytes=b"plain text") is True
    assert is_text_convertible_content_type("", sample_bytes=b"bin\x00data") is False
    assert is_text_convertible_content_type("", sample_bytes=b"\xff\xfeA\x00B\x00") is True
    assert is_text_convertible_content_type("application/vnd.api+json") is True
    assert is_text_convertible_content_type("application/octet-stream") is False

    # BOM, XML encoding, http-equiv meta, invalid header fallback, and byte detection
    assert resolve_charset_and_decode(b"") == ("", "utf-8")
    utf8_bom_text, utf8_bom_enc = resolve_charset_and_decode(b"\xef\xbb\xbfhello")
    assert (utf8_bom_text, utf8_bom_enc) == ("hello", "utf-8")
    assert extract_declared_charset(None, b"\xff\xfeA\x00") == "utf-16-le"
    assert extract_declared_charset(None, b"\xfe\xff\x00A") == "utf-16-be"
    assert (
        extract_declared_charset(
            "application/xml", b'<?xml version="1.0" encoding="iso-8859-1"?><r/>'
        )
        == "cp1252"
    )
    assert (
        extract_declared_charset(
            "text/html; charset=bogus-codec-999",
            b'<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">',
        )
        == "cp1251"
    )
    # Short non-UTF-8 falls back to windows-1252; longer non-UTF-8 exercises charset_normalizer
    short_decoded, short_enc = resolve_charset_and_decode(b"caf\xe9")
    assert short_decoded == "café" and short_enc == "windows-1252"
    long_cyrillic = "Привет мир, это достаточно длинный текст на русском языке. ".encode(
        "windows-1251"
    )
    long_decoded, long_enc = resolve_charset_and_decode(long_cyrillic, "text/plain")
    assert "Привет" in long_decoded and long_enc != ""

    # Header extraction across httpx.Headers, dict, custom getter, and missing
    assert extract_header_case_insensitive(httpx.Headers({"X-Test": "1"}), "x-test") == "1"
    assert (
        extract_header_case_insensitive({"Content-Type": "text/plain"}, "content-type")
        == "text/plain"
    )
    assert extract_header_case_insensitive({"Other": "v"}, "content-type") is None

    class _CustomHeaders:
        def get(self, key: str) -> str | None:
            return "val" if key == "X-Case" else None

    assert extract_header_case_insensitive(_CustomHeaders(), "X-Case") == "val"
    assert extract_header_case_insensitive(object(), "X-Case") is None

    # Bounded streaming: plain truncation, zlib-wrapped deflate, corrupt brotli, corrupt deflate
    plain_resp = httpx.Response(200, stream=httpx.ByteStream(b"0123456789"))
    truncated = await read_bounded_response_bytes(
        plain_resp,
        max_bytes=5,
        on_too_large=lambda n: ValueError(f"too large {n}"),
        on_decode_error=ValueError,
        truncate_on_limit=True,
    )
    assert truncated == b"0123456789"[:5]

    wrapped_deflate = zlib.compress(b"zlib wrapped body")
    deflate_resp = httpx.Response(
        200,
        headers={"Content-Encoding": "deflate"},
        stream=httpx.ByteStream(wrapped_deflate),
    )
    decoded_deflate = await read_bounded_response_bytes(
        deflate_resp,
        max_bytes=1024,
        on_too_large=lambda n: ValueError(f"too large {n}"),
        on_decode_error=ValueError,
    )
    assert decoded_deflate == b"zlib wrapped body"

    bad_br_resp = httpx.Response(
        200, headers={"Content-Encoding": "br"}, stream=httpx.ByteStream(b"\xff\xff\xffbad")
    )
    with pytest.raises(ValueError, match="Invalid brotli"):
        await read_bounded_response_bytes(
            bad_br_resp,
            max_bytes=1024,
            on_too_large=lambda n: ValueError(f"too large {n}"),
            on_decode_error=ValueError,
        )

    bad_gz_resp = httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"not-gzip")
    )
    with pytest.raises(ValueError, match="Invalid gzip"):
        await read_bounded_response_bytes(
            bad_gz_resp,
            max_bytes=1024,
            on_too_large=lambda n: ValueError(f"too large {n}"),
            on_decode_error=ValueError,
        )

    # Content negotiation + sanitize_original_url_header non-ASCII/IDNA/userinfo/IPv6 paths
    assert format_to_mime_type("unknown") == "application/octet-stream"
    assert parse_accept_header(None) == "text"
    assert parse_accept_header("application/octet-stream") == "raw"
    assert parse_accept_headers(None) == []
    assert parse_accept_headers(["text/html;q=0.9, text/markdown", "text/html"]) == [
        "html",
        "markdown",
    ]
    assert sanitize_original_url_header("https://example.com/ascii") == "https://example.com/ascii"
    idna_sanitized = sanitize_original_url_header("https://münchen.de:8443/café?q=über#top")
    assert idna_sanitized.isascii()
    assert "xn--mnchen-3ya.de:8443" in idna_sanitized
    assert "%C3%A9" in idna_sanitized
    userinfo_sanitized = sanitize_original_url_header("https://alíce:päss@example.com/résumé")
    assert userinfo_sanitized.isascii()
    assert "@example.com/" in userinfo_sanitized
    user_only = sanitize_original_url_header("https://alíce@example.com/p")
    assert user_only.isascii() and "@example.com/p" in user_only
    ipv6_sanitized = sanitize_original_url_header("https://[2001:db8::1]:8080/café")
    assert ipv6_sanitized.isascii() and "[2001:db8::1]:8080" in ipv6_sanitized


@pytest.mark.unit
@pytest.mark.parametrize(
    ("codec", "bom", "declared_ct"),
    [
        ("utf-16-le", b"\xff\xfe", "text/plain; charset=utf-16le"),
        ("utf-16-be", b"\xfe\xff", "text/plain; charset=utf-16be"),
        ("utf-32-le", b"\xff\xfe\x00\x00", "text/plain; charset=utf-32le"),
        ("utf-32-be", b"\x00\x00\xfe\xff", "text/plain; charset=utf-32be"),
    ],
)
def test_utf16_and_utf32_strip_bom_and_allow_bomless_declared_charset(
    codec: str, bom: bytes, declared_ct: str
) -> None:
    """`A-13`: UTF-16/UTF-32 BOM is stripped and BOM-less declared UTF-16/32 skips NUL rejection."""
    from downloader.content_encoding import is_text_convertible_content_type

    raw_bomless = "Hello Café 東京".encode(codec)
    raw_with_bom = bom + raw_bomless

    # 1. BOM-prefixed bytes strip U+FEFF both with and without Content-Type charset
    decoded_auto, _ = resolve_charset_and_decode(raw_with_bom, "text/plain")
    assert decoded_auto == "Hello Café 東京"
    assert "\ufeff" not in decoded_auto

    decoded_decl, _ = resolve_charset_and_decode(raw_with_bom, declared_ct)
    assert decoded_decl == "Hello Café 東京"
    assert "\ufeff" not in decoded_decl

    # 2. BOM-less bytes with declared UTF-16/32 charset pass NUL sniff and decode cleanly
    assert is_text_convertible_content_type(declared_ct, sample_bytes=raw_bomless) is True
    decoded_bomless, _ = resolve_charset_and_decode(raw_bomless, declared_ct)
    assert decoded_bomless == "Hello Café 東京"
    assert convert_content(raw_bomless, declared_ct, "text") == "Hello Café 東京"

    # 3. Bare charset=utf-16 / charset=utf-32 without BOM defaults to little-endian instead of UnicodeError
    if codec == "utf-16-le":
        assert is_text_convertible_content_type(
            "text/plain; charset=utf-16", sample_bytes=raw_bomless
        )
        assert (
            resolve_charset_and_decode(raw_bomless, "text/plain; charset=utf-16")[0]
            == "Hello Café 東京"
        )
    if codec == "utf-32-le":
        assert is_text_convertible_content_type(
            "text/plain; charset=utf-32", sample_bytes=raw_bomless
        )
        assert (
            resolve_charset_and_decode(raw_bomless, "text/plain; charset=utf-32")[0]
            == "Hello Café 東京"
        )

"""Pure charset resolution, OCR MIME classification, and text media-type guards."""

from __future__ import annotations

import codecs
import html
import re
from collections.abc import Mapping
from typing import Any

from charset_normalizer import from_bytes as detect_charset_from_bytes

SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/gif",
    }
)

_BOM_CHARSETS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)

_WHATWG_LATIN1_ALIASES = frozenset(
    {
        "iso-8859-1",
        "iso8859-1",
        "iso_8859-1",
        "iso_8859-1:1987",
        "latin1",
        "latin-1",
        "l1",
        "cp819",
        "ibm819",
    }
)

_TEXTUAL_APPLICATION_TYPES = frozenset(
    {
        "application/xhtml+xml",
        "application/xml",
        "application/json",
        "application/ld+json",
        "application/rss+xml",
        "application/atom+xml",
        "application/javascript",
        "application/x-javascript",
        "application/ecmascript",
        "application/x-www-form-urlencoded",
        "application/sql",
        "application/graphql",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
    }
)

_MARKUP_MEDIA_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "text/xml",
        "application/xml",
    }
)

_META_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_META_CONTENT_CHARSET_RE = re.compile(
    rb"<meta\b[^>]*?\bcontent\s*=\s*[\"'][^\"']*?\bcharset\s*=\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_XML_ENCODING_RE = re.compile(
    rb"^<\?xml\b[^>]*?\bencoding\s*=\s*[\"']([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_HEADER_CHARSET_RE = re.compile(
    r";\s*charset\s*=\s*(?:\"([^\"]+)\"|'([^']+)'|([^\s;]+))",
    re.IGNORECASE,
)


def normalize_media_type(content_type: str | None) -> str:
    """Return lowercase MIME type stripped of parameters (`text/html; charset=utf-8` -> `text/html`)."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def is_ocr_image_content_type(content_type: str | None) -> bool:
    """Return True if the MIME type is a supported raster image for OCR."""
    if not content_type:
        return False
    return normalize_media_type(content_type) in SUPPORTED_IMAGE_MIME_TYPES


def format_ocr_markdown(text: str, source_url: str | None = None) -> str:
    """Wrap extracted OCR text into clean Markdown paragraphs."""
    body = text.strip()
    if not body:
        return ""
    if source_url:
        return f"<!-- OCR extracted from {source_url} -->\n\n{body}"
    return body


def format_ocr_html(text: str, source_url: str | None = None) -> str:
    """Wrap HTML-escaped OCR text into a `<pre>` HTML block."""
    body = html.escape(text.strip())
    if not body:
        return "<pre></pre>"
    if source_url:
        safe_url = html.escape(source_url, quote=True)
        return f"<!-- OCR extracted from {safe_url} -->\n<pre>{body}</pre>"
    return f"<pre>{body}</pre>"


def _is_markup_media_type(content_type: str | None) -> bool:
    media_type = normalize_media_type(content_type)
    return media_type in _MARKUP_MEDIA_TYPES or media_type.endswith("+xml")


def _has_binary_nul_bytes(sample: bytes) -> bool:
    """Detect NUL bytes in unlabeled bodies while allowing UTF-16/UTF-32 BOM-prefixed text."""
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in sample[:4096]


def _extract_header_charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    header_match = _HEADER_CHARSET_RE.search(content_type)
    if not header_match:
        return None
    raw_param = header_match.group(1) or header_match.group(2) or header_match.group(3)
    return _normalize_codec_name(raw_param)


def is_text_convertible_content_type(
    content_type: str | None,
    *,
    allow_ocr_images: bool = False,
    sample_bytes: bytes | None = None,
) -> bool:
    """Return True if `content_type` can be safely decoded as text/HTML/markdown (or OCR image)."""
    media_type = normalize_media_type(content_type)
    header_charset = _extract_header_charset(content_type)
    is_wide_unicode = header_charset is not None and header_charset.startswith(("utf-16", "utf-32"))
    no_nul = is_wide_unicode or sample_bytes is None or not _has_binary_nul_bytes(sample_bytes)

    if allow_ocr_images and is_ocr_image_content_type(media_type):
        return True
    if media_type in {"", "unknown"}:
        return no_nul
    if media_type.startswith("text/"):
        return no_nul
    return media_type in _TEXTUAL_APPLICATION_TYPES or media_type.endswith(
        ("+xml", "+json", "+text", "+yaml")
    )


def _normalize_codec_name(raw_charset: str | None) -> str | None:
    """Resolve an HTTP/HTML charset label to a Python codec name, applying WHATWG Latin-1 mapping."""
    if not raw_charset:
        return None
    cleaned = raw_charset.strip().strip("\"'").lower()
    if not cleaned:
        return None
    candidate = "windows-1252" if cleaned in _WHATWG_LATIN1_ALIASES else cleaned
    try:
        return codecs.lookup(candidate).name
    except LookupError:
        return None


def _match_bom(head_bytes: bytes) -> tuple[bytes, str] | None:
    for prefix, codec in _BOM_CHARSETS:
        if head_bytes.startswith(prefix):
            return prefix, codec
    return None


def _detect_bom_charset(head_bytes: bytes) -> str | None:
    matched = _match_bom(head_bytes)
    return matched[1] if matched is not None else None


def _detect_meta_charset(head_bytes: bytes) -> str | None:
    if not head_bytes:
        return None
    window = head_bytes[:4096]
    for pattern in (_META_CHARSET_RE, _META_CONTENT_CHARSET_RE, _XML_ENCODING_RE):
        meta_match = pattern.search(window)
        if meta_match:
            resolved = _normalize_codec_name(meta_match.group(1).decode("ascii", "ignore"))
            if resolved is not None:
                # Per WHATWG prescan, ASCII <meta>/<?xml> tags cannot represent a real UTF-16 stream.
                return "utf-8" if resolved.startswith("utf-16") else resolved
    return None


def extract_declared_charset(
    content_type: str | None,
    head_bytes: bytes = b"",
) -> str | None:
    """Resolve charset from BOM, HTTP Content-Type header, or HTML/XML meta declaration."""
    bom_charset = _detect_bom_charset(head_bytes)
    if bom_charset is not None:
        return bom_charset

    header_charset = _extract_header_charset(content_type)
    if header_charset is not None:
        return header_charset

    if not _is_markup_media_type(content_type):
        return None
    return _detect_meta_charset(head_bytes)


def _decode_bom_or_declared(
    content: bytes,
    content_type: str | None,
) -> tuple[str, str] | None:
    bom_match = _match_bom(content[:4])
    if bom_match is not None:
        bom_prefix, bom_codec = bom_match
        if bom_codec == "utf-8-sig":
            return content.decode("utf-8-sig", errors="replace"), "utf-8"
        return content[len(bom_prefix) :].decode(bom_codec, errors="replace"), bom_codec

    declared = extract_declared_charset(content_type, content[:4096])
    if declared is None:
        return None
    effective = {"utf-16": "utf-16-le", "utf-32": "utf-32-le"}.get(declared, declared)
    canonical = "utf-8" if effective == "utf-8-sig" else effective
    return content.decode(effective, errors="replace"), canonical


def resolve_charset_and_decode(
    content: bytes,
    content_type: str | None = None,
) -> tuple[str, str]:
    """Decode `content` once using BOM/header/meta charset, strict UTF-8, or byte detection."""
    if not content:
        return "", "utf-8"

    explicit = _decode_bom_or_declared(content, content_type)
    if explicit is not None:
        return explicit

    try:
        return content.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    if len(content) >= 24:
        best = detect_charset_from_bytes(content).best()
        if best is not None and best.encoding:
            detected = _normalize_codec_name(best.encoding)
            if detected is not None:
                return content.decode(detected, errors="replace"), detected

    return content.decode("windows-1252", errors="replace"), "windows-1252"


def extract_header_case_insensitive(headers: Any, name: str) -> str | None:
    """Read a header value case-insensitively from an HTTP headers container or string mapping."""
    target = name.lower()
    if isinstance(headers, Mapping):
        for key, val in headers.items():
            if isinstance(key, str) and key.lower() == target and isinstance(val, (str, int)):
                return str(val)

    getter = getattr(headers, "get", None)
    if callable(getter):
        val = getter(target)
        if val is None:
            val = getter(name)
        if isinstance(val, (str, int)):
            return str(val)
    return None


def __getattr__(name: str) -> Any:
    if name in {
        "_BROTLI_SLAB_LIMIT",
        "_BoundedDecodeBuffer",
        "make_stateless_cookies",
        "read_bounded_response_bytes",
    }:
        from . import wire_stream

        return getattr(wire_stream, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

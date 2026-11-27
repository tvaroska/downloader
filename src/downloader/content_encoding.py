"""Stateless cookies, bounded decompression, charset resolution, and text media-type guards."""

from __future__ import annotations

import codecs
import re
import zlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import brotli
import httpx
from charset_normalizer import from_bytes as detect_charset_from_bytes

from .transformers.ocr import is_ocr_image_content_type

_DECOMPRESS_SLICE_BYTES = 65536
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


class _RejectAllCookiePolicy(DefaultCookiePolicy):
    """Cookie policy rejecting every incoming Set-Cookie and outgoing Cookie replay."""

    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


def make_stateless_cookies() -> httpx.Cookies:
    """Return an httpx.Cookies container whose underlying CookieJar stores nothing."""
    return httpx.Cookies(CookieJar(policy=_RejectAllCookiePolicy()))


def normalize_media_type(content_type: str | None) -> str:
    """Return lowercase MIME type stripped of parameters (`text/html; charset=utf-8` -> `text/html`)."""
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


def _is_markup_media_type(content_type: str | None) -> bool:
    media_type = normalize_media_type(content_type)
    return media_type in _MARKUP_MEDIA_TYPES or media_type.endswith("+xml")


def _has_binary_nul_bytes(sample: bytes) -> bool:
    """Detect NUL bytes in unlabeled bodies while allowing UTF-16/UTF-32 BOM-prefixed text."""
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return False
    return b"\x00" in sample[:4096]


def is_text_convertible_content_type(
    content_type: str | None,
    *,
    allow_ocr_images: bool = False,
    sample_bytes: bytes | None = None,
) -> bool:
    """Return True if `content_type` can be safely decoded as text/HTML/markdown (or OCR image)."""
    media_type = normalize_media_type(content_type)
    no_nul = sample_bytes is None or not _has_binary_nul_bytes(sample_bytes)

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


def _detect_bom_charset(head_bytes: bytes) -> str | None:
    for prefix, codec in _BOM_CHARSETS:
        if head_bytes.startswith(prefix):
            return codec
    return None


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

    if content_type:
        header_match = _HEADER_CHARSET_RE.search(content_type)
        if header_match:
            raw_param = header_match.group(1) or header_match.group(2) or header_match.group(3)
            resolved = _normalize_codec_name(raw_param)
            if resolved is not None:
                return resolved

    if not _is_markup_media_type(content_type):
        return None
    return _detect_meta_charset(head_bytes)


def resolve_charset_and_decode(
    content: bytes,
    content_type: str | None = None,
) -> tuple[str, str]:
    """Decode `content` once using BOM/header/meta charset, strict UTF-8, or byte detection."""
    if not content:
        return "", "utf-8"

    declared = extract_declared_charset(content_type, content[:4096])
    if declared is not None:
        canonical = "utf-8" if declared == "utf-8-sig" else declared
        return content.decode(declared, errors="replace"), canonical

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
    """Read a header value case-insensitively from `httpx.Headers` or any string mapping."""
    if isinstance(headers, httpx.Headers):
        val = headers.get(name)
        return val if isinstance(val, str) else None

    target = name.lower()
    if isinstance(headers, Mapping):
        for key, val in headers.items():
            if isinstance(key, str) and key.lower() == target and isinstance(val, (str, int)):
                return str(val)
        return None

    getter = getattr(headers, "get", None)
    if callable(getter):
        val = getter(target)
        if val is None:
            val = getter(name)
        if isinstance(val, (str, int)):
            return str(val)
    return None


def _extract_content_encoding_list(headers: Any) -> list[str]:
    """Parse Content-Encoding header into ordered lowercase codec tokens."""
    raw = extract_header_case_insensitive(headers, "content-encoding")
    if not raw:
        return []
    tokens = (tok.strip().lower() for tok in raw.split(","))
    return [tok for tok in tokens if tok and tok != "identity"]


async def _iter_wire_chunks(response: Any) -> AsyncIterator[bytes]:
    """Yield raw wire chunks from an `httpx.Response` stream."""
    async for chunk in response.aiter_raw():
        if chunk:
            yield chunk


@dataclass
class _BoundedDecodeBuffer:
    max_bytes: int
    truncate_on_limit: bool
    on_too_large: Callable[[int], Exception]
    chunks: list[bytes] = field(default_factory=list)
    raw_total: int = 0
    decoded_total: int = 0

    def record_raw(self, raw_chunk: bytes) -> None:
        self.raw_total += len(raw_chunk)
        if self.raw_total > self.max_bytes and not self.truncate_on_limit:
            raise self.on_too_large(self.raw_total)

    def step_limit(self) -> int:
        return max(1, min(_DECOMPRESS_SLICE_BYTES, self.max_bytes - self.decoded_total + 1))

    def append_decoded(self, piece: bytes) -> bool:
        if not piece:
            return False
        next_total = self.decoded_total + len(piece)
        if next_total > self.max_bytes:
            if self.truncate_on_limit:
                keep = self.max_bytes - self.decoded_total
                if keep > 0:
                    self.chunks.append(piece[:keep])
                self.decoded_total = self.max_bytes
                return True
            raise self.on_too_large(next_total)
        self.chunks.append(piece)
        self.decoded_total = next_total
        return self.truncate_on_limit and self.decoded_total >= self.max_bytes

    def build_bytes(self) -> bytes:
        return b"".join(self.chunks)


# C brotli grows slabs (32752, then +65520 = 98272) while allocated < output_buffer_limit.
_BROTLI_SLAB_LIMIT = 32_752


async def _decode_brotli_stream(
    response: Any,
    buf: _BoundedDecodeBuffer,
    on_decode_error: Callable[[str], Exception],
) -> bytes:
    br_dec = brotli.Decompressor()
    try:
        async for raw_chunk in _iter_wire_chunks(response):
            buf.record_raw(raw_chunk)
            piece = br_dec.process(
                raw_chunk, output_buffer_limit=min(buf.step_limit(), _BROTLI_SLAB_LIMIT)
            )
            if buf.append_decoded(piece):
                return buf.build_bytes()
            while not br_dec.can_accept_more_data() and not br_dec.is_finished():
                piece = br_dec.process(
                    b"", output_buffer_limit=min(buf.step_limit(), _BROTLI_SLAB_LIMIT)
                )
                if buf.append_decoded(piece):
                    return buf.build_bytes()
        # Drain any remaining buffered output after wire EOF before checking `is_finished()`.
        while not br_dec.is_finished():
            piece = br_dec.process(
                b"", output_buffer_limit=min(buf.step_limit(), _BROTLI_SLAB_LIMIT)
            )
            if not piece:
                break
            if buf.append_decoded(piece):
                return buf.build_bytes()
    except brotli.error as exc:
        raise on_decode_error(f"Invalid brotli stream: {exc}") from exc
    if not br_dec.is_finished():
        raise on_decode_error("Invalid brotli stream: truncated or incomplete input")
    return buf.build_bytes()


def _init_deflate_decompressor(
    raw_chunk: bytes,
    step_limit: int,
) -> tuple[zlib._Decompress, bytes]:
    # RFC 9110 §8.4.1.2: try zlib-wrapped deflate first, then raw deflate.
    probe = zlib.decompressobj(zlib.MAX_WBITS)
    try:
        first_piece = probe.decompress(raw_chunk, step_limit)
        return probe, first_piece
    except zlib.error:
        fallback = zlib.decompressobj(-zlib.MAX_WBITS)
        return fallback, fallback.decompress(raw_chunk, step_limit)


async def _decode_zlib_stream(
    response: Any,
    codec: str,
    buf: _BoundedDecodeBuffer,
    on_decode_error: Callable[[str], Exception],
) -> bytes:
    zlib_dec: zlib._Decompress | None = (
        zlib.decompressobj(16 + zlib.MAX_WBITS) if codec in {"gzip", "x-gzip"} else None
    )
    try:
        async for raw_chunk in _iter_wire_chunks(response):
            buf.record_raw(raw_chunk)
            if zlib_dec is None:
                zlib_dec, first_piece = _init_deflate_decompressor(raw_chunk, buf.step_limit())
                if buf.append_decoded(first_piece):
                    return buf.build_bytes()
                tail = zlib_dec.unconsumed_tail
            else:
                tail = raw_chunk

            while tail:
                piece = zlib_dec.decompress(tail, buf.step_limit())
                if buf.append_decoded(piece):
                    return buf.build_bytes()
                next_tail = zlib_dec.unconsumed_tail
                if next_tail == tail:
                    break
                tail = next_tail

        if zlib_dec is None or not zlib_dec.eof:
            raise on_decode_error(f"Invalid {codec} stream: truncated or incomplete input")
        buf.append_decoded(zlib_dec.flush())
    except zlib.error as exc:
        raise on_decode_error(f"Invalid {codec} stream: {exc}") from exc
    return buf.build_bytes()


async def read_bounded_response_bytes(
    response: Any,
    *,
    max_bytes: int,
    on_too_large: Callable[[int], Exception],
    on_decode_error: Callable[[str], Exception],
    truncate_on_limit: bool = False,
) -> bytes:
    """Stream and decompress an HTTP response while bounding both compressed and decoded bytes."""
    encodings = _extract_content_encoding_list(getattr(response, "headers", {}))
    if len(encodings) > 1 or (
        encodings and encodings[0] not in {"gzip", "x-gzip", "deflate", "br"}
    ):
        raise on_decode_error(f"Unsupported Content-Encoding: {', '.join(encodings)}")

    buf = _BoundedDecodeBuffer(
        max_bytes=max_bytes,
        truncate_on_limit=truncate_on_limit,
        on_too_large=on_too_large,
    )
    codec = encodings[0] if encodings else None
    if codec is None:
        async for chunk in _iter_wire_chunks(response):
            if buf.append_decoded(chunk):
                break
        return buf.build_bytes()
    if codec == "br":
        return await _decode_brotli_stream(response, buf, on_decode_error)
    return await _decode_zlib_stream(response, codec, buf, on_decode_error)

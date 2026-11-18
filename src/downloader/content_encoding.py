"""Stateless cookies, bounded decompression, charset resolution, and text media-type guards."""

from __future__ import annotations

import codecs
import re
import zlib
from collections.abc import AsyncIterator, Callable
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import brotli
import httpx
from charset_normalizer import from_bytes as detect_charset_from_bytes

from .transformers.ocr import is_ocr_image_content_type

_DECOMPRESS_SLICE_BYTES = 65536

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

    if allow_ocr_images and is_ocr_image_content_type(media_type):
        return True

    if media_type in {"", "unknown"}:
        if sample_bytes is not None and _has_binary_nul_bytes(sample_bytes):
            return False
        return True

    if media_type.startswith("text/"):
        return sample_bytes is None or not _has_binary_nul_bytes(sample_bytes)

    if media_type in _TEXTUAL_APPLICATION_TYPES:
        return True

    if media_type.endswith(("+xml", "+json", "+text", "+yaml")):
        return True

    return False


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


def extract_declared_charset(
    content_type: str | None,
    head_bytes: bytes = b"",
) -> str | None:
    """Resolve charset from BOM, HTTP Content-Type header, or HTML/XML meta declaration."""
    if head_bytes.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if head_bytes.startswith(b"\xff\xfe\x00\x00"):
        return "utf-32-le"
    if head_bytes.startswith(b"\x00\x00\xfe\xff"):
        return "utf-32-be"
    if head_bytes.startswith(b"\xff\xfe"):
        return "utf-16-le"
    if head_bytes.startswith(b"\xfe\xff"):
        return "utf-16-be"

    if content_type:
        m = _HEADER_CHARSET_RE.search(content_type)
        if m:
            raw_param = m.group(1) or m.group(2) or m.group(3)
            resolved = _normalize_codec_name(raw_param)
            if resolved is not None:
                return resolved

    if head_bytes:
        window = head_bytes[:4096]
        for pattern in (_META_CHARSET_RE, _META_CONTENT_CHARSET_RE, _XML_ENCODING_RE):
            meta_match = pattern.search(window)
            if meta_match:
                resolved = _normalize_codec_name(meta_match.group(1).decode("ascii", "ignore"))
                if resolved is not None:
                    return resolved

    return None


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


def _extract_content_encoding_list(headers: Any) -> list[str]:
    """Parse Content-Encoding header into ordered lowercase codec tokens."""
    raw: str | None = None
    try:
        for key, val in dict(headers).items():
            if isinstance(key, str) and key.lower() == "content-encoding" and isinstance(val, str):
                raw = val
                break
    except Exception:
        raw = None

    if raw is None:
        getter = getattr(headers, "get", None)
        if callable(getter):
            candidate = getter("content-encoding") or getter("Content-Encoding")
            if isinstance(candidate, str):
                raw = candidate

    if not raw:
        return []
    return [
        tok.strip().lower()
        for tok in raw.split(",")
        if tok.strip() and tok.strip().lower() != "identity"
    ]


async def _iter_wire_chunks(response: Any) -> AsyncIterator[bytes]:
    """Yield raw wire chunks when available (httpx.Response), or fallback for unit-test mocks."""
    if isinstance(response, httpx.Response):
        if response.is_stream_consumed:
            inner_stream: Any = response.stream
            while inner_stream is not None and not isinstance(inner_stream, bytes):
                inner_stream = getattr(inner_stream, "_stream", None)
            if isinstance(inner_stream, bytes):
                for offset in range(0, len(inner_stream), _DECOMPRESS_SLICE_BYTES):
                    piece = inner_stream[offset : offset + _DECOMPRESS_SLICE_BYTES]
                    if piece:
                        yield piece
                return
        async for chunk in response.aiter_raw():
            if chunk:
                yield chunk
        return

    aiter_raw = getattr(response, "aiter_raw", None)
    if callable(aiter_raw):
        raw_iter = aiter_raw()
        if hasattr(raw_iter, "__aiter__"):
            async for chunk in raw_iter:
                if chunk:
                    yield chunk
            return

    async for chunk in response.aiter_bytes():
        if chunk:
            yield chunk


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

    codec = encodings[0] if encodings else None
    out_chunks: list[bytes] = []
    raw_total = 0
    decoded_total = 0

    def _append_decoded(piece: bytes) -> bool:
        nonlocal decoded_total
        if not piece:
            return False
        next_total = decoded_total + len(piece)
        if next_total > max_bytes:
            if truncate_on_limit:
                keep = max_bytes - decoded_total
                if keep > 0:
                    out_chunks.append(piece[:keep])
                decoded_total = max_bytes
                return True
            raise on_too_large(next_total)
        out_chunks.append(piece)
        decoded_total = next_total
        return truncate_on_limit and decoded_total >= max_bytes

    if codec is None:
        async for chunk in _iter_wire_chunks(response):
            if _append_decoded(chunk):
                break
        return b"".join(out_chunks)

    if codec == "br":
        br_dec = brotli.Decompressor()
        try:
            async for raw_chunk in _iter_wire_chunks(response):
                raw_total += len(raw_chunk)
                if raw_total > max_bytes and not truncate_on_limit:
                    raise on_too_large(raw_total)

                step_limit = max(1, min(_DECOMPRESS_SLICE_BYTES, max_bytes - decoded_total + 1))
                piece = br_dec.process(raw_chunk, output_buffer_limit=step_limit)
                if _append_decoded(piece):
                    return b"".join(out_chunks)

                while not br_dec.can_accept_more_data() and not br_dec.is_finished():
                    step_limit = max(1, min(_DECOMPRESS_SLICE_BYTES, max_bytes - decoded_total + 1))
                    piece = br_dec.process(b"", output_buffer_limit=step_limit)
                    if _append_decoded(piece):
                        return b"".join(out_chunks)
        except brotli.error as exc:
            raise on_decode_error(f"Invalid brotli stream: {exc}") from exc
        return b"".join(out_chunks)

    zlib_dec: zlib._Decompress | None = (
        zlib.decompressobj(16 + zlib.MAX_WBITS) if codec in {"gzip", "x-gzip"} else None
    )
    try:
        async for raw_chunk in _iter_wire_chunks(response):
            raw_total += len(raw_chunk)
            if raw_total > max_bytes and not truncate_on_limit:
                raise on_too_large(raw_total)

            if zlib_dec is None:
                # RFC 9110 §8.4.1.2: try zlib-wrapped deflate first, then raw deflate.
                probe = zlib.decompressobj(zlib.MAX_WBITS)
                step_limit = max(1, min(_DECOMPRESS_SLICE_BYTES, max_bytes - decoded_total + 1))
                try:
                    first_piece = probe.decompress(raw_chunk, step_limit)
                    zlib_dec = probe
                except zlib.error:
                    zlib_dec = zlib.decompressobj(-zlib.MAX_WBITS)
                    first_piece = zlib_dec.decompress(raw_chunk, step_limit)
                if _append_decoded(first_piece):
                    return b"".join(out_chunks)
                tail = zlib_dec.unconsumed_tail
            else:
                tail = raw_chunk

            while tail:
                step_limit = max(1, min(_DECOMPRESS_SLICE_BYTES, max_bytes - decoded_total + 1))
                piece = zlib_dec.decompress(tail, step_limit)
                if _append_decoded(piece):
                    return b"".join(out_chunks)
                next_tail = zlib_dec.unconsumed_tail
                if next_tail == tail:
                    break
                tail = next_tail

        if zlib_dec is not None:
            flushed = zlib_dec.flush()
            _append_decoded(flushed)
    except zlib.error as exc:
        raise on_decode_error(f"Invalid {codec} stream: {exc}") from exc

    return b"".join(out_chunks)

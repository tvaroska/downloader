"""Stateless HTTP cookie policy and bounded wire-stream decompression adapters."""

from __future__ import annotations

import zlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any

import brotli
import httpx

from .content_encoding import extract_header_case_insensitive

_DECOMPRESS_SLICE_BYTES = 65536
# C brotli grows slabs (32752, then +65520 = 98272) while allocated < output_buffer_limit.
_BROTLI_SLAB_LIMIT = 32_752


class _RejectAllCookiePolicy(DefaultCookiePolicy):
    """Cookie policy rejecting every incoming Set-Cookie and outgoing Cookie replay."""

    def set_ok(self, cookie: Cookie, request: Any) -> bool:
        return False

    def return_ok(self, cookie: Cookie, request: Any) -> bool:
        return False


def make_stateless_cookies() -> httpx.Cookies:
    """Return an httpx.Cookies container whose underlying CookieJar stores nothing."""
    return httpx.Cookies(CookieJar(policy=_RejectAllCookiePolicy()))


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


def _drain_zlib_tail(
    zlib_dec: zlib._Decompress,
    tail: bytes,
    buf: _BoundedDecodeBuffer,
) -> bool:
    while tail:
        piece = zlib_dec.decompress(tail, buf.step_limit())
        if buf.append_decoded(piece):
            return True
        next_tail = zlib_dec.unconsumed_tail
        if next_tail == tail:
            break
        tail = next_tail
    return False


def _finalize_zlib_stream(
    zlib_dec: zlib._Decompress | None,
    codec: str,
    buf: _BoundedDecodeBuffer,
    on_decode_error: Callable[[str], Exception],
) -> bytes:
    if zlib_dec is None:
        raise on_decode_error(f"Invalid {codec} stream: truncated or incomplete input")
    while True:
        piece = zlib_dec.decompress(b"", buf.step_limit())
        if not piece:
            break
        if buf.append_decoded(piece):
            return buf.build_bytes()
    if not zlib_dec.eof:
        raise on_decode_error(f"Invalid {codec} stream: truncated or incomplete input")
    return buf.build_bytes()


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
            if _drain_zlib_tail(zlib_dec, tail, buf):
                return buf.build_bytes()
        return _finalize_zlib_stream(zlib_dec, codec, buf, on_decode_error)
    except zlib.error as exc:
        raise on_decode_error(f"Invalid {codec} stream: {exc}") from exc


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

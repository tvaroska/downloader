"""Unit tests for bounded streaming decompression across HTTPClient and RobotsChecker (`R3`)."""

from __future__ import annotations

import gzip
import hashlib
import tracemalloc
import zlib
from collections.abc import AsyncIterator

import brotli
import httpx
import pytest

from downloader.crawling.robots import RobotsChecker
from downloader.http_client import ContentTooLargeError, HTTPClient, HTTPClientError


class _TrackingRedirectStream(httpx.AsyncByteStream):
    """Async stream recording whether any redirect body chunk was read or closed."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.chunks_read = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.chunks_read += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.unit
@pytest.mark.asyncio
class TestBoundedDecompression:
    async def test_brotli_bomb_bounded_by_max_download_size(self) -> None:
        # 16 MiB of zeros compresses to < 1 KiB in brotli; cap is 64 KiB.
        compressed_bomb = brotli.compress(b"\x00" * (16 * 1024 * 1024), quality=6)
        assert len(compressed_bomb) < 1024

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Content-Encoding": "br",
                },
                stream=httpx.ByteStream(compressed_bomb),
                request=request,
            )

        async with HTTPClient(
            max_download_size=64 * 1024,
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ContentTooLargeError) as exc_info:
                await client.download("https://example.com/bomb.txt")
            assert exc_info.value.max_size == 64 * 1024

    async def test_gzip_and_deflate_bombs_bounded_and_valid_streams_decoded(self) -> None:
        gzip_bomb = gzip.compress(b"A" * (2 * 1024 * 1024), compresslevel=9)
        valid_deflate = zlib.compress(b"hello compressed world")[2:-4]  # raw RFC 1951 deflate

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/gzip-bomb":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                    stream=httpx.ByteStream(gzip_bomb),
                    request=request,
                )
            if request.url.path == "/raw-deflate":
                return httpx.Response(
                    200,
                    headers={"Content-Type": "text/plain", "Content-Encoding": "deflate"},
                    stream=httpx.ByteStream(valid_deflate),
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "Content-Encoding": "br, gzip"},
                stream=httpx.ByteStream(b"stacked"),
                request=request,
            )

        async with HTTPClient(
            max_download_size=32 * 1024,
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ContentTooLargeError):
                await client.download("https://example.com/gzip-bomb")

            decoded, _ = await client.download("https://example.com/raw-deflate")
            assert decoded == b"hello compressed world"

            with pytest.raises(HTTPClientError, match="Unsupported Content-Encoding"):
                await client.download("https://example.com/stacked")

    async def test_robots_fetch_truncates_brotli_bomb_at_512_kib(self) -> None:
        robots_payload = b"User-agent: *\nDisallow: /private\n" + (b"# pad\n" * 200_000)
        compressed = brotli.compress(robots_payload, quality=1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "Content-Encoding": "br"},
                stream=httpx.ByteStream(compressed),
                request=request,
            )

        async with RobotsChecker(transport=httpx.MockTransport(handler)) as checker:
            status_code, body = await checker._default_fetch("https://example.com/robots.txt")
            assert status_code == 200
            assert len(body.encode("utf-8")) <= 512 * 1024
            assert body.startswith("User-agent: *\nDisallow: /private")

    async def test_raw_compressed_byte_budget_enforced(self) -> None:
        """Raw compressed bytes exceeding `max_download_size` raise `ContentTooLargeError` (`R93`)."""
        # Incompressible SHA-256 digests wrapped in gzip stay larger than the 512-byte limit.
        incompressible = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(30))
        gz = gzip.compress(incompressible, compresslevel=1)
        assert len(gz) > 512

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "application/octet-stream", "Content-Encoding": "gzip"},
                stream=httpx.ByteStream(gz),
                request=request,
            )

        async with HTTPClient(
            max_download_size=512,
            transport=httpx.MockTransport(handler),
        ) as client:
            with pytest.raises(ContentTooLargeError):
                await client.download("https://example.com/raw-over-cap")

    async def test_redirect_body_bomb_is_not_read_or_decompressed(self) -> None:
        """Followed 3xx responses close unread without decompressing bomb bodies (`A-1`)."""
        bomb = brotli.compress(b"\x00" * (16 * 1024 * 1024), quality=6)
        assert len(bomb) < 1024
        hop1_stream = _TrackingRedirectStream([bomb[:200], bomb[200:]])
        hop2_stream = _TrackingRedirectStream([bomb[:200], bomb[200:]])
        seen_headers: dict[str, dict[str, str]] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url_str = str(request.url)
            seen_headers[url_str] = dict(request.headers)
            if url_str == "https://example.com/start":
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://test.com/hop2",
                        "Content-Encoding": "br",
                    },
                    stream=hop1_stream,
                    request=request,
                )
            if url_str == "https://test.com/hop2":
                return httpx.Response(
                    307,
                    headers={
                        "Location": "/final",
                        "Content-Encoding": "br",
                        "Set-Cookie": "redir_sid=ok; Path=/",
                    },
                    stream=hop2_stream,
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain; charset=utf-8"},
                stream=httpx.ByteStream(b"safe destination"),
                request=request,
            )

        tracemalloc.start()
        try:
            async with HTTPClient(
                max_download_size=64 * 1024,
                transport=httpx.MockTransport(handler),
            ) as client:
                content, meta = await client.download(
                    "https://example.com/start",
                    headers={"Authorization": "Bearer caller-tok"},
                )
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert content == b"safe destination"
        assert meta["url"] == "https://test.com/final"
        assert hop1_stream.chunks_read == 0
        assert hop1_stream.closed is True
        assert hop2_stream.chunks_read == 0
        assert hop2_stream.closed is True
        assert peak_bytes < 2 * 1024 * 1024
        assert "authorization" in seen_headers["https://example.com/start"]
        assert "authorization" not in seen_headers["https://test.com/hop2"]
        assert "redir_sid=ok" in seen_headers["https://test.com/final"].get("cookie", "")

        loop_streams: list[_TrackingRedirectStream] = []

        def loop_handler(request: httpx.Request) -> httpx.Response:
            st = _TrackingRedirectStream([bomb])
            loop_streams.append(st)
            return httpx.Response(
                302,
                headers={"Location": "/loop", "Content-Encoding": "br"},
                stream=st,
                request=request,
            )

        async with HTTPClient(
            max_redirects=2,
            max_download_size=64 * 1024,
            transport=httpx.MockTransport(loop_handler),
        ) as capped_client:
            with pytest.raises(HTTPClientError, match="redirect"):
                await capped_client.download("https://example.com/loop")
        assert len(loop_streams) == 3
        assert all(st.chunks_read == 0 and st.closed for st in loop_streams)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_truncated_gzip_is_rejected() -> None:
    """Truncated gzip and brotli streams raise `HTTPClientError` instead of returning partial bytes (`R112`)."""
    full_gz = gzip.compress(b"complete payload body " * 200)
    truncated_gz = full_gz[:-12]
    full_br = brotli.compress(b"complete brotli body " * 200)
    truncated_br = full_br[:-8]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/trunc-gz":
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                stream=httpx.ByteStream(truncated_gz),
                request=request,
            )
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Encoding": "br"},
            stream=httpx.ByteStream(truncated_br),
            request=request,
        )

    async with HTTPClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(HTTPClientError, match="gzip|Truncated|Decompression"):
            await client.download("https://example.com/trunc-gz")
        with pytest.raises(HTTPClientError, match="brotli|Truncated|Decompression"):
            await client.download("https://example.com/trunc-br")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("encoding_header", "compressed_payload"),
    [
        ("br", brotli.compress(b"\x00" * (4 * 1024 * 1024), quality=6)),
        ("gzip", gzip.compress(b"A" * (4 * 1024 * 1024), compresslevel=9)),
        ("deflate", zlib.compress(b"B" * (4 * 1024 * 1024), level=9)),
        ("deflate", zlib.compress(b"C" * (4 * 1024 * 1024), level=9)[2:-4]),
    ],
    ids=["brotli", "gzip", "deflate-zlib", "deflate-raw"],
)
async def test_decompression_step_output_bounded_for_brotli_gzip_and_deflate(
    monkeypatch: pytest.MonkeyPatch,
    encoding_header: str,
    compressed_payload: bytes,
) -> None:
    """Every decompression step bounds output to <= 64 KiB and stops at cap + 1 byte (`T-2`)."""
    import downloader.content_encoding as ce

    cap = 100 * 1024  # > 64 KiB so both initial and loop decompression steps execute
    seen_piece_sizes: list[int] = []
    orig_append = ce._BoundedDecodeBuffer.append_decoded

    def _spy_append(self: ce._BoundedDecodeBuffer, piece: bytes) -> bool:
        if piece:
            seen_piece_sizes.append(len(piece))
        return orig_append(self, piece)

    monkeypatch.setattr(ce._BoundedDecodeBuffer, "append_decoded", _spy_append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Encoding": encoding_header},
            stream=httpx.ByteStream(compressed_payload),
            request=request,
        )

    async with HTTPClient(
        max_download_size=cap,
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ContentTooLargeError) as exc_info:
            await client.download("https://example.com/step-bounded-bomb")

    assert seen_piece_sizes, "Expected at least one decoded step"
    assert max(seen_piece_sizes) <= 65_536
    assert exc_info.value.actual_size is not None
    assert exc_info.value.actual_size <= cap + 65_536 + 1
    if encoding_header != "br":
        assert exc_info.value.actual_size == cap + 1

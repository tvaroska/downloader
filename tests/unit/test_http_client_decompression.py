"""Unit tests for bounded streaming decompression across HTTPClient and RobotsChecker (`R3`)."""

from __future__ import annotations

import gzip
import hashlib
import zlib

import brotli
import httpx
import pytest

from downloader.crawling.robots import RobotsChecker
from downloader.http_client import ContentTooLargeError, HTTPClient, HTTPClientError


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

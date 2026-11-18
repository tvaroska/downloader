"""Unit tests for bounded streaming decompression across HTTPClient and RobotsChecker (`R3`)."""

from __future__ import annotations

import gzip
import zlib

import brotli
import httpx
import pytest

from src.downloader.crawling.robots import RobotsChecker
from src.downloader.http_client import ContentTooLargeError, HTTPClient, HTTPClientError


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

        client = HTTPClient(max_download_size=64 * 1024)
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(ContentTooLargeError) as exc_info:
                await client.download("https://example.com/bomb.txt")
            assert exc_info.value.max_size == 64 * 1024
        finally:
            await client.close()

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

        client = HTTPClient(max_download_size=32 * 1024)
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(ContentTooLargeError):
                await client.download("https://example.com/gzip-bomb")

            decoded, _ = await client.download("https://example.com/raw-deflate")
            assert decoded == b"hello compressed world"

            with pytest.raises(HTTPClientError, match="Unsupported Content-Encoding"):
                await client.download("https://example.com/stacked")
        finally:
            await client.close()

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

        checker = RobotsChecker()
        checker._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            status_code, body = await checker._default_fetch("https://example.com/robots.txt")
            assert status_code == 200
            assert len(body.encode("utf-8")) <= 512 * 1024
            assert body.startswith("User-agent: *\nDisallow: /private")
        finally:
            await checker.close()

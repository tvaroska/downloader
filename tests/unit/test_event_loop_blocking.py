"""Unit tests verifying CPU-bound HTML parsing and conversion do not block the event loop."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from downloader import content_converter
from downloader.content_converter import MAX_HTML_PARSE_BYTES, convert_content
from downloader.http_client import ContentTooLargeError
from downloader.models.responses import ResponseMetadata
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversion_does_not_block_loop() -> None:
    """An asyncio heartbeat continues ticking smoothly during multi-MB HTML conversion."""
    content_converter._js_heavy_cache.clear()
    content_converter._static_html_cache.clear()

    # Build a >2 MiB HTML document with thousands of DOM nodes so BeautifulSoup/markdownify
    # takes measurable CPU time.
    paragraph = "<p><strong>Item</strong>: Example paragraph with <a href='https://example.com'>link</a>.</p>"
    body_chunks = paragraph * 23_000
    large_html = (
        "<html><head><meta property='og:title' content='Big'>"
        "<meta property='og:description' content='Doc'></head>"
        f"<body><main>{body_chunks}</main></body></html>"
    ).encode()
    assert len(large_html) > 2 * 1024 * 1024

    url = "https://example.com/large-static-doc"
    metadata: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "url": url,
        "size": len(large_html),
        "content_type": "text/html; charset=utf-8",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    deps = ConversionDeps(renderer=AsyncMock())

    max_gap = 0.0
    ticks = 0
    stop = asyncio.Event()

    async def heartbeat() -> None:
        nonlocal max_gap, ticks
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            max_gap = max(max_gap, now - last)
            last = now
            ticks += 1

    started = time.perf_counter()
    hb_task = asyncio.create_task(heartbeat())
    try:
        md_res = await convert(
            ConversionRequest(
                url=url,
                output_format="markdown",
                content=large_html,
                metadata=metadata,
            ),
            deps,
        )
        text_res = await convert(
            ConversionRequest(
                url=url,
                output_format="text",
                content=large_html,
                metadata=metadata,
            ),
            deps,
        )
    finally:
        stop.set()
        await hb_task
    total_elapsed = time.perf_counter() - started

    assert b"Example paragraph" in md_res.body
    assert b"Example paragraph" in text_res.body
    assert ticks >= 20, f"Heartbeat only ticked {ticks} times in {total_elapsed:.2f}s"
    # If conversion ran synchronously on the event loop, max_gap would be ~total_elapsed/2
    # (multi-second stall). Offloaded to the bounded executor, the loop keeps ticking.
    assert max_gap < 1.0, f"Event loop stalled for {max_gap:.3f}s during HTML conversion"
    assert max_gap < total_elapsed / 4, (
        f"Event loop gap {max_gap:.3f}s too large relative to {total_elapsed:.3f}s conversion"
    )


@pytest.mark.unit
def test_html_parse_size_guard_rejects_oversized_payload() -> None:
    """HTML payloads exceeding MAX_HTML_PARSE_BYTES fail fast with ContentTooLargeError."""
    oversized = b"x" * (MAX_HTML_PARSE_BYTES + 1)
    with pytest.raises(ContentTooLargeError):
        convert_content(oversized, "text/html; charset=utf-8", "markdown")

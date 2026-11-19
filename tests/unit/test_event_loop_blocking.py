"""Unit tests verifying CPU-bound HTML parsing and conversion do not block the event loop."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader import content_converter
from downloader.content_converter import MAX_HTML_PARSE_BYTES, convert_content
from downloader.http_client import ContentTooLargeError
from downloader.models.responses import ResponseMetadata
from downloader.ports import BrowserRenderer
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert
from downloader.services.multi_format import MultiFormatRequest, process_multiple_formats


@pytest.mark.unit
@pytest.mark.asyncio
async def test_conversion_does_not_block_loop() -> None:
    """An asyncio heartbeat continues ticking smoothly during HTML conversion (`R94`)."""
    content_converter._js_heavy_cache.clear()
    content_converter._static_html_cache.clear()

    # Keep payload fast (< 1s serial) while still exercising executor offloading clearly.
    paragraph = "<p><strong>Item</strong>: Example paragraph with <a href='https://example.com'>link</a>.</p>"
    body_chunks = paragraph * 2_500
    large_html = (
        "<html><head><meta property='og:title' content='Big'>"
        "<meta property='og:description' content='Doc'></head>"
        f"<body><main>{body_chunks}</main></body></html>"
    ).encode()

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
            await asyncio.sleep(0.005)
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
    assert ticks >= 5, f"Heartbeat only ticked {ticks} times in {total_elapsed:.2f}s"
    assert max_gap < 0.5, f"Event loop stalled for {max_gap:.3f}s during HTML conversion"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_multi_format_detection_does_not_block_loop() -> None:
    """Multi-format auto-render detection offloads `should_use_playwright_for_html` to executor (`R99`)."""
    content_converter._js_heavy_cache.clear()
    content_converter._static_html_cache.clear()

    html = (
        b"<html><head><meta property='og:title' content='T'>"
        b"<meta property='og:description' content='D'></head>"
        b"<body><article><p>Static multi-format article body.</p></article></body></html>"
    )
    metadata: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "url": "https://example.com/multi-offload",
        "size": len(html),
        "content_type": "text/html; charset=utf-8",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    renderer = MagicMock(spec=BrowserRenderer)
    renderer.render_html = AsyncMock(return_value=None)

    offloaded_funcs: list[str] = []
    real_run = content_converter.run_in_html_executor

    async def _spy_run(func, *args, **kwargs):
        offloaded_funcs.append(getattr(func, "__name__", repr(func)))
        return await real_run(func, *args, **kwargs)

    with patch("downloader.services.multi_format.run_in_html_executor", side_effect=_spy_run):
        out = await process_multiple_formats(
            MultiFormatRequest(
                url="https://example.com/multi-offload",
                formats=["html", "text", "markdown"],
                content=html,
                metadata=metadata,
            ),
            ConversionDeps(renderer=renderer),
        )

    assert "should_use_playwright_for_html" in offloaded_funcs
    assert "Static multi-format article body." in out["text/plain"]


@pytest.mark.unit
def test_html_parse_size_guard_rejects_oversized_payload() -> None:
    """HTML payloads exceeding MAX_HTML_PARSE_BYTES fail fast with ContentTooLargeError."""
    oversized = b"x" * (MAX_HTML_PARSE_BYTES + 1)
    with pytest.raises(ContentTooLargeError):
        convert_content(oversized, "text/html; charset=utf-8", "markdown")

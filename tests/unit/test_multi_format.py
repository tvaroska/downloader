"""Unit tests for multi-format shared rendering and failure handling (`R99`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader import content_converter
from downloader.models.responses import ResponseMetadata
from downloader.ports import BrowserRenderer, PDFGeneratorError
from downloader.services.content_processor import ConversionDeps
from downloader.services.multi_format import MultiFormatRequest, process_multiple_formats


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failed_shared_render_is_not_retried_per_format() -> None:
    """When auto-render fails once for multi-format, formats fall back to static HTML without retrying (`R99`)."""
    content_converter._js_heavy_cache.clear()
    content_converter._static_html_cache.clear()

    spa_html = (
        b"<html><head><title>App</title></head>"
        b"<body><div id='root'><p>Static fallback content</p></div></body></html>"
    )
    metadata: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "url": "https://example.com/spa-multi",
        "size": len(spa_html),
        "content_type": "text/html; charset=utf-8",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }

    renderer = MagicMock(spec=BrowserRenderer)
    renderer.render_html = AsyncMock(side_effect=PDFGeneratorError("navigation failed"))
    renderer.convert_fallback = AsyncMock(side_effect=AssertionError("must not retry per format"))

    result = await process_multiple_formats(
        MultiFormatRequest(
            url="https://example.com/spa-multi",
            formats=["html", "text", "markdown"],
            content=spa_html,
            metadata=metadata,
        ),
        ConversionDeps(renderer=renderer),
    )

    assert "errors" not in result
    assert "Static fallback content" in result["text/html"]
    assert "Static fallback content" in result["text/plain"]
    assert "Static fallback content" in result["text/markdown"]
    assert renderer.render_html.await_count == 1
    assert renderer.convert_fallback.await_count == 0

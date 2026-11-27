"""Unit tests for multi-format shared rendering and failure handling (`R99`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from downloader import content_converter
from downloader.dependencies import get_browser_renderer, get_http_client, get_robots_checker
from downloader.main import app
from downloader.models.responses import ResponseMetadata
from downloader.ports import (
    BrowserCapacityError,
    BrowserRenderer,
    PDFGeneratorError,
    ScreenshotGeneratorError,
)
from downloader.services.content_processor import ConversionDeps
from downloader.services.multi_format import (
    MultiFormatRequest,
    convert_multiple_formats,
    process_multiple_formats,
)


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_formats_failed_raises_first_typed_error_and_maps_route_status() -> None:
    """When every requested format fails, multi-format raises the first typed error instead of 200 (`A-5`)."""
    renderer = MagicMock(spec=BrowserRenderer)
    renderer.render_pdf = AsyncMock(
        side_effect=PDFGeneratorError("HTTP 404: Not Found", status_code=404)
    )
    renderer.render_screenshot = AsyncMock(
        side_effect=ScreenshotGeneratorError("HTTP 404: Not Found", status_code=404)
    )

    with pytest.raises(PDFGeneratorError) as exc_info:
        await convert_multiple_formats(
            MultiFormatRequest(
                url="https://example.com/missing",
                formats=["pdf", "screenshot"],
            ),
            ConversionDeps(renderer=renderer),
        )
    assert exc_info.value.status_code == 404

    # Route boundary maps all-failed browser multi-format 404 -> HTTP 404 (not 200 {"errors": ...})
    renderer.render_html = AsyncMock(return_value=b"<html><body>Alive</body></html>")
    html_body = (
        b"<html><body><article><p>"
        + (b"Alive paragraph content. " * 25)
        + b"</p></article></body></html>"
    )
    mock_robots = MagicMock()
    mock_robots.check_allowed = AsyncMock(return_value=None)
    mock_http = MagicMock()
    mock_http.download = AsyncMock(
        return_value=(
            html_body,
            {
                "status_code": 200,
                "headers": {"content-type": "text/html"},
                "url": "https://example.com/partial",
                "size": len(html_body),
                "content_type": "text/html",
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )
    )

    app.dependency_overrides[get_browser_renderer] = lambda: renderer
    app.dependency_overrides[get_robots_checker] = lambda: mock_robots
    app.dependency_overrides[get_http_client] = lambda: mock_http
    try:
        with TestClient(app) as client:
            r_404 = client.get(
                "/https://example.com/missing",
                headers={"Accept": "application/pdf, image/png"},
            )
            assert r_404.status_code == 404
            assert r_404.json()["detail"]["error_type"] == "http_error"

            # Partial success (text succeeds, PDF fails) still returns HTTP 200 with `errors`
            r_partial = client.get(
                "/https://example.com/partial",
                headers={"Accept": "text/plain, application/pdf"},
            )
            assert r_partial.status_code == 200
            body = r_partial.json()
            assert "Alive" in body["text/plain"]
            assert "pdf processing failed:" in body["errors"]["application/pdf"]

            # All browser formats hitting BrowserCapacityError -> HTTP 503
            renderer.render_pdf = AsyncMock(side_effect=BrowserCapacityError("busy"))
            renderer.render_screenshot = AsyncMock(side_effect=BrowserCapacityError("busy"))
            r_503 = client.get(
                "/https://example.com/busy",
                headers={"Accept": "application/pdf, image/png"},
            )
            assert r_503.status_code == 503
            assert r_503.json()["detail"]["error_type"] == "service_unavailable"
    finally:
        app.dependency_overrides.pop(get_browser_renderer, None)
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_http_client, None)

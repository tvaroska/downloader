"""Regression tests for `R77`: Playwright and browser-pool errors must survive auto-render fallback."""

from __future__ import annotations

import asyncio
import json

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from downloader.browser.renderer import LocalBrowserRenderer
from downloader.errors import classify_domain_error
from downloader.pdf_generator import PlaywrightPDFGenerator, set_shared_pdf_generator
from downloader.ports import (
    BrowserAcquireTimeoutError,
    BrowserPoolError,
    PDFGeneratorError,
    RenderTimeoutError,
    ScreenshotGeneratorError,
)
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert
from downloader.services.multi_format import MultiFormatRequest, convert_multiple_formats
from tests.fixtures.mock_fixtures import make_response_metadata
from tests.fixtures.playwright_fakes import FakeBrowserPool, FakePage

_SMALL_SPA_HTML = b"<html><body><div id='root'></div><p>Static fallback body</p></body></html>"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc_factory", "fail_at"),
    [
        (lambda: PlaywrightTimeoutError("Page.goto: Timeout 10000ms exceeded."), "goto"),
        (lambda: PlaywrightError("net::ERR_CONNECTION_RESET"), "goto"),
        (lambda: BrowserPoolError("Chromium process crashed"), "acquire"),
        (lambda: BrowserAcquireTimeoutError("Browser pool acquire timeout"), "acquire"),
    ],
    ids=["playwright-timeout", "playwright-error", "browser-pool-error", "pool-acquire-timeout"],
)
@pytest.mark.parametrize("output_format", ["text", "markdown", "html", "multi_format"])
async def test_auto_fallback_survives_playwright_navigation_errors(
    exc_factory,
    fail_at: str,
    output_format: str,
) -> None:
    """Auto-render fallbacks must serve static content with `rendered_with_js is False` on browser errors."""
    exc = exc_factory()
    if fail_at == "acquire":
        fake_pool = FakeBrowserPool(acquire_error=exc)
    else:
        fake_pool = FakeBrowserPool(page=FakePage(goto_error=exc))

    from typing import Any, cast

    gen = PlaywrightPDFGenerator(pool_size=1)
    gen.pool = cast(Any, fake_pool)
    set_shared_pdf_generator(gen)
    try:
        renderer = LocalBrowserRenderer(asyncio.Semaphore(1))
        meta = make_response_metadata(
            url="https://example.com/spa",
            content_type="text/html; charset=utf-8",
            size=len(_SMALL_SPA_HTML),
        )

        async def _http_fetcher():
            return _SMALL_SPA_HTML, meta

        deps = ConversionDeps(renderer=renderer, http_fetcher=_http_fetcher)

        if output_format == "multi_format":
            res = await convert_multiple_formats(
                MultiFormatRequest(
                    url="https://example.com/spa",
                    formats=["text", "markdown", "html"],
                ),
                deps,
            )
            payload = json.loads(res.body.decode("utf-8"))
            assert "errors" not in payload
            assert "Static fallback body" in payload["text/plain"]
            assert "Static fallback body" in payload["text/markdown"]
            assert "Static fallback body" in payload["text/html"]
            assert res.rendered_with_js is False
        else:
            res = await convert(
                ConversionRequest(
                    url="https://example.com/spa",
                    output_format=output_format,  # type: ignore[arg-type]
                    content=_SMALL_SPA_HTML,
                    metadata=meta,
                ),
                deps,
            )
            assert "Static fallback body" in res.text
            assert res.rendered_with_js is False
    finally:
        set_shared_pdf_generator(None)


@pytest.mark.unit
def test_browser_and_screenshot_domain_error_classifications() -> None:
    """Verify `errors.py` rows for `RenderTimeoutError`, `BrowserPoolError`, and `ScreenshotGeneratorError`."""
    rt = classify_domain_error(RenderTimeoutError("navigation timed out"))
    assert rt.status_code == 408
    assert rt.error_type == "timeout_error"
    assert rt.retryable is True

    bp = classify_domain_error(BrowserPoolError("pool exhausted"))
    assert bp.status_code == 503
    assert bp.error_type == "service_unavailable"
    assert bp.retryable is True

    ss = classify_domain_error(ScreenshotGeneratorError("capture failed"))
    assert ss.status_code == 500
    assert ss.error_type == "screenshot_generation_error"

    upstream_5xx = classify_domain_error(PDFGeneratorError("HTTP 503", status_code=503))
    assert upstream_5xx.status_code == 502
    assert upstream_5xx.error_type == "http_error"
    assert upstream_5xx.retryable is True

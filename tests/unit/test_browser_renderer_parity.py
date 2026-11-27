"""Port contract parity tests for `LocalBrowserRenderer` vs `RemoteBrowserRenderer` (`R89`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from downloader.browser.renderer import LocalBrowserRenderer, RemoteBrowserRenderer
from downloader.errors import classify_domain_error
from downloader.models.browser_actions import BrowserActionError
from downloader.ports import (
    BrowserCapacityError,
    BrowserPoolError,
    ContentConversionError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    PDFGeneratorError,
    RenderTimeoutError,
    RobotsDisallowedError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
)
from downloader.validation import DNSResolutionError, SSRFProtectionError, URLValidationError
from downloader.worker import process_worker_browser_task
from downloader.worker_queue import WorkerQueue
from tests.fixtures.in_memory_ports import InMemoryWorkQueue
from tests.fixtures.mock_fixtures import FakeRedis


@pytest.mark.unit
@pytest.mark.port
@pytest.mark.parametrize(
    ("kind", "exc_factory"),
    [
        ("render_html", lambda: DNSResolutionError("nxdomain.invalid")),
        ("render_html", lambda: SSRFProtectionError("Blocked private IP 127.0.0.1")),
        ("render_html", lambda: URLValidationError("Malformed URL")),
        ("render_html", lambda: RobotsDisallowedError("https://example.com/private")),
        ("render_html", lambda: CrawlDelayExceededError("example.com", 7.5)),
        ("render_html", lambda: SelectorTimeoutError("#missing", 4321)),
        ("render_html", lambda: BrowserActionError("Action click failed")),
        ("render_html", lambda: ContentTooLargeError(1024, 2048)),
        ("render_html", lambda: ContentConversionError("Parser error")),
        ("render_html", lambda: BrowserCapacityError("At capacity")),
        ("render_html", lambda: BrowserPoolError("Pool down")),
        ("render_html", lambda: RenderTimeoutError("Navigation timed out")),
        ("pdf", lambda: PDFGeneratorError("HTTP 404: Not Found", status_code=404)),
        ("pdf", lambda: PDFGeneratorError("HTTP 503: Unavailable", status_code=503)),
        ("pdf", lambda: PDFGeneratorError("Chromium print crash")),
        ("screenshot", lambda: ScreenshotGeneratorError("Viewport capture error")),
    ],
    ids=[
        "dns_resolution",
        "ssrf",
        "validation",
        "robots",
        "crawl_delay",
        "selector_timeout",
        "browser_action",
        "content_too_large",
        "conversion",
        "browser_capacity",
        "browser_pool",
        "render_timeout",
        "pdf_upstream_404",
        "pdf_upstream_503",
        "pdf_internal",
        "screenshot_internal",
    ],
)
async def test_local_and_remote_agree_on_error_classification(kind: str, exc_factory) -> None:
    """Local and remote `BrowserRenderer` adapters produce identical HTTP classifications."""
    local_exc = exc_factory()
    remote_exc_source = exc_factory()

    local_renderer = LocalBrowserRenderer(asyncio.Semaphore(2))
    queue = InMemoryWorkQueue()
    remote_renderer = RemoteBrowserRenderer(queue, timeout=5.0)
    worker_sem = asyncio.Semaphore(2)
    domain_errors = (
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        SelectorTimeoutError,
        BrowserActionError,
        ContentTooLargeError,
        ContentConversionError,
        BrowserCapacityError,
        BrowserPoolError,
        RenderTimeoutError,
        PDFGeneratorError,
        ScreenshotGeneratorError,
    )

    async def _run_local() -> Exception:
        with (
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
                new=AsyncMock(side_effect=local_exc),
            ),
            patch(
                "downloader.pdf_generator.generate_pdf_from_url",
                new=AsyncMock(side_effect=local_exc),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
                new=AsyncMock(side_effect=local_exc),
            ),
        ):
            with pytest.raises(domain_errors) as exc_info:
                if kind == "pdf":
                    await local_renderer.render_pdf("https://example.com")
                elif kind == "screenshot":
                    await local_renderer.render_screenshot("https://example.com")
                else:
                    await local_renderer.render_html(
                        "https://example.com",
                        wait_for_selector="#missing",
                        fail_fast=True,
                    )
            return exc_info.value

    async def _run_remote() -> Exception:
        async def _worker_once() -> None:
            task = await queue.pop_browser_task(timeout=1.0)
            assert task is not None
            with (
                patch(
                    "downloader.worker.render_html_with_playwright",
                    new=AsyncMock(side_effect=remote_exc_source),
                ),
                patch(
                    "downloader.worker.generate_pdf_from_url",
                    new=AsyncMock(side_effect=remote_exc_source),
                ),
                patch(
                    "downloader.worker.generate_screenshot_from_url",
                    new=AsyncMock(side_effect=remote_exc_source),
                ),
            ):
                await process_worker_browser_task(
                    task,
                    worker_queue=queue,
                    pdf_semaphore=worker_sem,
                )

        worker_task = asyncio.create_task(_worker_once())
        try:
            with pytest.raises(domain_errors) as exc_info:
                if kind == "pdf":
                    await remote_renderer.render_pdf("https://example.com")
                elif kind == "screenshot":
                    await remote_renderer.render_screenshot("https://example.com")
                else:
                    await remote_renderer.render_html(
                        "https://example.com",
                        wait_for_selector="#missing",
                        fail_fast=True,
                    )
            return exc_info.value
        finally:
            await worker_task

    raised_local = await _run_local()
    raised_remote = await _run_remote()
    assert isinstance(raised_local, type(local_exc))

    local_cls = classify_domain_error(raised_local, context="download")
    remote_cls = classify_domain_error(raised_remote, context="download")

    assert (remote_cls.status_code, remote_cls.error_type, remote_cls.retryable) == (
        local_cls.status_code,
        local_cls.error_type,
        local_cls.retryable,
    )
    if isinstance(local_exc, SelectorTimeoutError):
        assert isinstance(raised_remote, SelectorTimeoutError)
        assert raised_remote.timeout_ms == local_exc.timeout_ms


@pytest.mark.unit
@pytest.mark.port
async def test_remote_auto_render_degrades_when_saturated() -> None:
    """When remote RPC slots are full, `fail_fast=False` returns `None` while `fail_fast=True` raises."""
    fake_redis = FakeRedis()
    queue = WorkerQueue(fake_redis, max_remote_browser_calls=1)
    renderer = RemoteBrowserRenderer(queue, timeout=5.0)

    await queue._browser_inflight_semaphore.acquire()
    try:
        assert await renderer.render_html("https://example.com", fail_fast=False) is None
        assert await renderer.convert_fallback("https://example.com", "text") is None
        with pytest.raises(BrowserCapacityError):
            await renderer.render_html("https://example.com", fail_fast=True)
    finally:
        queue._browser_inflight_semaphore.release()

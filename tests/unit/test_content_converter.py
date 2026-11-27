"""Unit and port-contract tests for v0.8.0 conversion errors, size bounds, and adapters (`R44`-`R46`, `R49`, `R58`, `R61`)."""

from __future__ import annotations

import asyncio
import base64
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.browser.renderer import (
    LocalBrowserRenderer,
    RemoteBrowserRenderer,
    render_html_with_playwright,
)
from downloader.config import Settings
from downloader.content_converter import convert_content
from downloader.dependencies import (
    get_browser_renderer,
    get_domain_throttle,
    get_http_client,
    get_robots_checker,
)
from downloader.http_client import ContentConversionError, ContentTooLargeError
from downloader.job_manager import JobStatus
from downloader.main import app
from downloader.models.responses import ResponseMetadata
from downloader.pdf_generator import PlaywrightPDFGenerator, install_ssrf_route_guard
from downloader.ports import BrowserRenderer
from downloader.ratelimit_middleware import RateLimitMiddleware, reset_rate_limiter
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert
from downloader.services.multi_format import MultiFormatRequest, process_multiple_formats
from downloader.validation import EgressPolicy, URLValidationError
from downloader.worker_queue import BrowserTaskResponse
from tests.fixtures.in_memory_ports import InMemoryJobStore, InMemoryWorkQueue


@pytest.mark.unit
def test_conversion_failure_is_not_returned_as_raw_html(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Raise logged `ContentConversionError` (HTTP 500) instead of returning raw HTML (`R58`)."""
    raw_html = b"<html><body><script>secret()</script><h1>Heading</h1></body></html>"
    caplog.set_level(logging.ERROR, logger="downloader.content_converter")

    with (
        patch(
            "downloader.content_converter._convert_html_to_format",
            side_effect=RecursionError("maximum recursion depth exceeded"),
        ),
        pytest.raises(ContentConversionError, match="Failed to convert HTML"),
    ):
        convert_content(raw_html, "text/html", "text")

    assert any("HTML text conversion failed" in rec.message for rec in caplog.records)

    # Verify HTTP route surfaces 500 conversion_error and never leaks raw <html> tags as plaintext
    mock_http = MagicMock()
    mock_http.download = AsyncMock(
        return_value=(
            raw_html,
            {
                "status_code": 200,
                "headers": {"content-type": "text/html"},
                "url": "https://example.com/deep",
                "size": len(raw_html),
                "content_type": "text/html",
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )
    )
    mock_robots = MagicMock(check_allowed=AsyncMock(return_value=None))
    mock_throttle = MagicMock(wait_if_needed=AsyncMock(return_value=0.0))

    app.dependency_overrides[get_http_client] = lambda: mock_http
    app.dependency_overrides[get_robots_checker] = lambda: mock_robots
    app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
    try:
        with (
            patch(
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com/deep"),
            ),
            patch(
                "downloader.content_converter._convert_html_to_format",
                side_effect=RecursionError("deep tree"),
            ),
            TestClient(app) as client,
        ):
            resp = client.get("/https://example.com/deep", headers={"Accept": "text/plain"})
            assert resp.status_code == 500
            assert resp.json()["detail"]["error_type"] == "conversion_error"
            assert "<script>" not in resp.text
    finally:
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)


@pytest.mark.unit
async def test_rendered_output_size_cap_enforced_across_html_pdf_and_screenshot() -> None:
    """Enforce `max_download_size` (`ContentTooLargeError` -> HTTP 413) on browser outputs (`R49`)."""
    page = AsyncMock()
    page.url = "https://example.com/huge"
    page.goto = AsyncMock(return_value=MagicMock(status=200, status_text="OK", request=None))
    page.wait_for_load_state = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])
    page.content = AsyncMock(return_value="<html><body>" + ("X" * 512) + "</body></html>")
    page.pdf = AsyncMock(return_value=b"%PDF-" + (b"0" * 512))
    page.screenshot = AsyncMock(return_value=b"\x89PNG" + (b"0" * 512))

    context = AsyncMock()
    context.route = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    browser = MagicMock()
    pool = AsyncMock()
    pool.get_browser = AsyncMock(return_value=browser)
    pool.create_context = AsyncMock(return_value=context)
    pool.release_browser = AsyncMock()

    cfg = Settings()
    cfg.content.max_download_size = 128
    policy = EgressPolicy(cfg)

    gen = PlaywrightPDFGenerator(pool_size=1, egress_policy=policy)
    gen.pool = pool

    with pytest.raises(ContentTooLargeError):
        await render_html_with_playwright(
            "https://example.com/huge",
            generator=gen,
            egress_policy=policy,
        )
    with pytest.raises(ContentTooLargeError):
        await gen.generate_pdf("https://example.com/huge")
    with pytest.raises(ContentTooLargeError):
        await gen.generate_screenshot("https://example.com/huge")


@pytest.mark.unit
async def test_rendered_output_exactly_at_cap_is_accepted() -> None:
    """Exact `max_download_size` rendered bytes pass while `cap + 1` raises (`R93`)."""
    cap = 128
    html_exact = (
        "<html><body>" + ("A" * (cap - len("<html><body></body></html>"))) + "</body></html>"
    )
    assert len(html_exact.encode("utf-8")) == cap
    pdf_exact = b"%PDF-" + (b"0" * (cap - 5))
    png_exact = b"\x89PNG" + (b"0" * (cap - 4))
    assert len(pdf_exact) == cap
    assert len(png_exact) == cap

    page = AsyncMock()
    page.url = "https://example.com/exact"
    page.goto = AsyncMock(return_value=MagicMock(status=200, status_text="OK", request=None))
    page.wait_for_load_state = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])
    page.content = AsyncMock(return_value=html_exact)
    page.pdf = AsyncMock(return_value=pdf_exact)
    page.screenshot = AsyncMock(return_value=png_exact)

    context = AsyncMock()
    context.route = AsyncMock()
    context.on = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    pool = AsyncMock()
    pool.get_browser = AsyncMock(return_value=MagicMock())
    pool.create_context = AsyncMock(return_value=context)
    pool.release_browser = AsyncMock()

    cfg = Settings()
    cfg.content.max_download_size = cap
    policy = EgressPolicy(cfg)
    gen = PlaywrightPDFGenerator(pool_size=1, egress_policy=policy)
    gen.pool = pool

    assert (
        len(
            await render_html_with_playwright(
                "https://example.com/exact", generator=gen, egress_policy=policy
            )
        )
        == cap
    )
    assert len(await gen.generate_pdf("https://example.com/exact")) == cap
    assert len(await gen.generate_screenshot("https://example.com/exact")) == cap

    # One byte above cap must be rejected across all three rendered formats.
    page.content = AsyncMock(return_value=html_exact + "Z")
    page.pdf = AsyncMock(return_value=pdf_exact + b"0")
    page.screenshot = AsyncMock(return_value=png_exact + b"0")
    with pytest.raises(ContentTooLargeError):
        await render_html_with_playwright(
            "https://example.com/exact", generator=gen, egress_policy=policy
        )
    with pytest.raises(ContentTooLargeError):
        await gen.generate_pdf("https://example.com/exact")
    with pytest.raises(ContentTooLargeError):
        await gen.generate_screenshot("https://example.com/exact")


@pytest.mark.unit
async def test_install_ssrf_route_guard_requires_callable_route() -> None:
    """Reject contexts lacking `.route` instead of silently skipping SSRF interception (`R46`)."""

    class _ContextWithoutRoute:
        pass

    with pytest.raises(TypeError, match="callable route"):
        await install_ssrf_route_guard(_ContextWithoutRoute())


@pytest.mark.unit
def test_ratelimit_ignores_xff_when_client_is_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Do not trust spoofed `X-Forwarded-For` when `request.client` is `None`, and log reset failures (`R58`)."""
    mw = RateLimitMiddleware(app)

    fake_request = MagicMock()
    fake_request.client = None
    fake_request.headers = {"x-forwarded-for": "203.0.113.99"}

    assert mw._resolve_client_ip(fake_request) == "unknown"

    stub_limiter = MagicMock()
    stub_limiter._key_func = lambda _req: "fallback-peer"
    mw_with_fallback = RateLimitMiddleware(app, stub_limiter)
    assert mw_with_fallback._resolve_client_ip(fake_request) == "fallback-peer"

    from limits.storage import MemoryStorage as SyncMemoryStorage

    bad_limiter = MagicMock()
    bad_limiter._storage_uri = "memory://"
    bad_limiter._storage = MagicMock(spec=SyncMemoryStorage)
    bad_limiter._storage.reset = MagicMock(side_effect=RuntimeError("storage reset boom"))
    caplog.set_level(logging.DEBUG, logger="downloader.ratelimit_middleware")
    reset_rate_limiter(bad_limiter)
    assert any("reset skipped" in rec.message for rec in caplog.records)


@pytest.mark.unit
def test_deep_value_error_maps_to_500_not_400() -> None:
    """Only query parsing maps `ValueError` to 400; unexpected deep `ValueError` maps to 500 (`R61`)."""
    mock_robots = MagicMock(check_allowed=AsyncMock(return_value=None))
    mock_throttle = MagicMock(wait_if_needed=AsyncMock(return_value=0.0))
    mock_http = MagicMock(
        download=AsyncMock(side_effect=ValueError("unexpected internal unpack bug"))
    )

    app.dependency_overrides[get_robots_checker] = lambda: mock_robots
    app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
    app.dependency_overrides[get_http_client] = lambda: mock_http
    try:
        with (
            patch(
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com/bug"),
            ),
            TestClient(app) as client,
        ):
            r_500 = client.get("/https://example.com/bug", headers={"Accept": "text/plain"})
            assert r_500.status_code == 500
            assert r_500.json()["detail"]["error_type"] == "internal_error"

            r_400 = client.get("/https://example.com/bug?ocr_lang=bad;cmd")
            assert r_400.status_code == 400
            assert r_400.json()["detail"]["error_type"] == "validation_error"
    finally:
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)
        app.dependency_overrides.pop(get_http_client, None)


@pytest.mark.unit
async def test_multi_format_renders_dom_at_most_once_and_forwards_force_ocr() -> None:
    """Multi-format renders HTML once across `html,text,markdown` and forwards `force_ocr` (`R45`)."""
    mock_renderer = MagicMock(spec=BrowserRenderer)
    mock_renderer.render_html = AsyncMock(
        return_value=b"<html><body><article><h1>Single Render</h1><p>Body</p></article></body></html>"
    )
    deps = ConversionDeps(renderer=mock_renderer)

    out = await process_multiple_formats(
        MultiFormatRequest(
            url="https://example.com/spa",
            formats=["html", "markdown", "text"],
            force_render=True,
        ),
        deps,
    )
    assert mock_renderer.render_html.await_count == 1
    assert "Single Render" in out["text/html"]
    assert "# Single Render" in out["text/markdown"]
    assert "Single Render" in out["text/plain"]

    # Forwarding force_ocr=True on non-image HTML surfaces per-format validation errors cleanly
    html_meta: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html"},
        "url": "https://example.com/not-image",
        "size": 26,
        "content_type": "text/html",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    # Partial failure (json succeeds, text fails force_ocr) keeps `errors` map; all-failed raises (`A-5`)
    ocr_err_map = await process_multiple_formats(
        MultiFormatRequest(
            url="https://example.com/not-image",
            formats=["text", "json"],
            force_ocr=True,
            content=b"<html><body>No image</body></html>",
            metadata=html_meta,
        ),
        deps,
    )
    assert "application/json" in ocr_err_map
    assert "errors" in ocr_err_map
    assert "OCR requires an image content type" in ocr_err_map["errors"]["text/plain"]

    with pytest.raises(URLValidationError, match="OCR requires an image content type"):
        await process_multiple_formats(
            MultiFormatRequest(
                url="https://example.com/not-image",
                formats=["text", "markdown"],
                force_ocr=True,
                content=b"<html><body>No image</body></html>",
                metadata=html_meta,
            ),
            deps,
        )


@pytest.mark.port
async def test_in_memory_ports_and_remote_browser_renderer_parity() -> None:
    """Verify `InMemoryJobStore`, `InMemoryWorkQueue`, and `RemoteBrowserRenderer` contracts (`R44`, `R46`)."""
    store = InMemoryJobStore()
    job_id = await store.create_job({"urls": [{"url": "https://example.com"}]})
    info = await store.get_job_info(job_id)
    assert info is not None and info.status == JobStatus.PENDING

    await store.update_job_status(job_id, JobStatus.RUNNING, progress=50, processed_urls=1)
    await store.store_job_results(job_id, [{"url": "https://example.com", "success": True}], {})
    await store.update_job_status(job_id, JobStatus.COMPLETED, progress=100, successful_urls=1)
    res = await store.get_job_results(job_id)
    assert res is not None and len(res.results) == 1

    bg_job_id = await store.create_job({"urls": [{"url": "https://example.com"}]})

    async def _processor(_jid: str) -> tuple[list[dict[str, object]], dict[str, int]]:
        return [{"url": "https://example.com", "success": True}], {"successful_requests": 1}

    task = await store.start_background_job(bg_job_id, _processor)
    await task
    assert await store.wait_for_active_jobs(1.0) is True
    bg_info = await store.get_job_info(bg_job_id)
    assert bg_info is not None and bg_info.status == JobStatus.COMPLETED

    cancel_id = await store.create_job({"urls": [{"url": "https://example.com"}]})
    assert await store.cancel_job(cancel_id) is True

    queue = InMemoryWorkQueue()
    await queue.enqueue_batch_job(job_id, trace_headers={"traceparent": "00-abc"})
    popped = await queue.pop_batch_job(timeout=0.1)
    assert popped is not None and popped.job_id == job_id
    assert await queue.requeue_processing_jobs() == 1
    popped2 = await queue.pop_batch_job(timeout=0.1)
    assert popped2 is not None and popped2.job_id == job_id
    await queue.ack_batch_job(popped2.raw_payload)
    assert (await queue.get_queue_depths())["batch_processing"] == 0

    await queue.publish_heartbeat("worker-1", {"pid": 123}, ttl=30)
    assert await queue.has_heartbeat("worker-1") is True
    await queue.remove_heartbeat("worker-1")
    assert await queue.has_heartbeat("worker-1") is False

    # Drive RemoteBrowserRenderer against InMemoryWorkQueue worker consumer coroutine
    remote = RemoteBrowserRenderer(queue, timeout=5.0, max_output_bytes=1024)
    local = LocalBrowserRenderer(asyncio.Semaphore(1), max_output_bytes=1024)
    assert isinstance(remote, BrowserRenderer)
    assert isinstance(local, BrowserRenderer)

    async def _worker_loop() -> None:
        task = await queue.pop_browser_task(timeout=2.0)
        assert task is not None
        html = b"<html><body><main><h1>Remote Worker OK</h1></main></body></html>"
        await queue.publish_browser_result(
            BrowserTaskResponse(
                task_id=task.task_id,
                success=True,
                content_base64=base64.b64encode(html).decode("ascii"),
            )
        )

    worker_task = asyncio.create_task(_worker_loop())
    conv = await convert(
        ConversionRequest(
            url="https://example.com/remote", output_format="markdown", force_render=True
        ),
        ConversionDeps(renderer=remote),
    )
    await worker_task
    assert "# Remote Worker OK" in conv.text
    assert get_browser_renderer is not None

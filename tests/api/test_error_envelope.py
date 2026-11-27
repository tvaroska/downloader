"""API error envelope tests (`R40`)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.crawling.robots import RobotsDisallowedError
from downloader.crawling.throttle import CrawlDelayExceededError
from downloader.dependencies import get_http_client, get_scheduler_dependency
from downloader.errors import ErrorContext, classify_domain_error
from downloader.http_client import (
    ContentConversionError,
    ContentTooLargeError,
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    UnsupportedMediaTypeError,
)
from downloader.main import app
from downloader.models.browser_actions import BrowserActionError
from downloader.ports import (
    BrowserCapacityError,
    BrowserPoolError,
    PDFGeneratorError,
    RenderTimeoutError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
    WorkerTimeoutError,
)
from downloader.transformers.ocr import OCRError
from downloader.validation import DNSResolutionError, SSRFProtectionError, URLValidationError


@pytest.mark.unit
def test_validation_errors_use_error_envelope(api_client, env_no_auth) -> None:
    """URL/query validation errors return the documented `{detail: {success, error, error_type}}` envelope."""
    response = api_client.get("/https://example.com/page?unknown_param=1")
    assert response.status_code == 400
    body = response.json()
    assert "detail" in body
    detail = body["detail"]
    assert detail["success"] is False
    assert detail["error_type"] == "validation_error"
    assert isinstance(detail["error"], str) and detail["error"]


@pytest.mark.unit
def test_render_timeout_uses_worker_timeout_error_type() -> None:
    """`WorkerTimeoutError` maps to `504 worker_timeout` across download and render contexts (`R114`)."""
    for ctx in ("download", "render"):
        classified = classify_domain_error(WorkerTimeoutError("worker timed out"), context=ctx)
        assert classified.status_code == 504
        assert classified.error_type == "worker_timeout"
        assert classified.retryable is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("exc", "context", "expected_status", "expected_type", "expected_retryable"),
    [
        (SSRFProtectionError("private ip"), "download", 400, "validation_error", False),
        (DNSResolutionError("nxdomain.invalid"), "download", 502, "http_error", True),
        (URLValidationError("bad url"), "download", 400, "validation_error", False),
        (BrowserActionError("click failed"), "render", 400, "browser_action_error", False),
        (
            RobotsDisallowedError("https://example.com/no"),
            "download",
            403,
            "robots_disallowed",
            False,
        ),
        (
            CrawlDelayExceededError("https://example.com", 5.0),
            "download",
            429,
            "crawl_delay_exceeded",
            True,
        ),
        (SelectorTimeoutError("#x", 5000), "render", 408, "selector_timeout_error", True),
        (HTTPTimeoutError("timed out"), "download", 408, "timeout_error", True),
        (RenderTimeoutError("render timed out"), "render", 408, "timeout_error", True),
        (ContentTooLargeError(100, 200), "download", 413, "content_too_large", False),
        (UnsupportedMediaTypeError("audio/mpeg"), "download", 415, "unsupported_media_type", False),
        (OCRError("ocr failed"), "download", 422, "ocr_error", False),
        (
            HTTPClientError("HTTP 401: Unauthorized", status_code=401),
            "download",
            401,
            "http_error",
            False,
        ),
        (
            HTTPClientError("HTTP 403: Forbidden", status_code=403),
            "download",
            403,
            "http_error",
            False,
        ),
        (
            HTTPClientError("HTTP 404: Not Found", status_code=404),
            "download",
            404,
            "http_error",
            False,
        ),
        (
            HTTPClientError("HTTP 429: Too Many", status_code=429),
            "download",
            429,
            "http_error",
            True,
        ),
        (
            HTTPClientError("HTTP 503: Unavailable", status_code=503),
            "download",
            502,
            "http_error",
            True,
        ),
        (HTTPClientError("Connection refused"), "download", 502, "http_error", True),
        (PDFGeneratorError("HTTP 404", status_code=404), "download", 404, "http_error", False),
        (PDFGeneratorError("HTTP 429", status_code=429), "download", 429, "http_error", True),
        (PDFGeneratorError("HTTP 503", status_code=503), "download", 502, "http_error", True),
        (PDFGeneratorError("pdf crash"), "download", 500, "pdf_generation_error", True),
        (
            ScreenshotGeneratorError("shot crash"),
            "download",
            500,
            "screenshot_generation_error",
            True,
        ),
        (BrowserCapacityError("full"), "download", 503, "service_unavailable", True),
        (BrowserPoolError("pool dead"), "download", 503, "service_unavailable", True),
        (WorkerTimeoutError("worker dead"), "download", 504, "worker_timeout", True),
        (TimeoutError("deadline"), "download", 408, "timeout_error", True),
        (TimeoutError("deadline"), "render", 408, "timeout_error", True),
        (TimeoutError("deadline"), "batch", 408, "timeout_error", True),
        (ContentConversionError("bad html"), "download", 500, "conversion_error", False),
        (DownloadError("download broke"), "download", 500, "download_error", True),
        (RuntimeError("boom"), "download", 500, "internal_error", True),
    ],
)
def test_retryable_flag_table(
    exc: Exception,
    context: ErrorContext,
    expected_status: int,
    expected_type: str,
    expected_retryable: bool,
) -> None:
    """Verify exact status, error_type, and retryable flag across all domain errors (`R93`, `R114`)."""
    classified = classify_domain_error(exc, context=context)
    assert classified.status_code == expected_status
    assert classified.error_type == expected_type
    assert classified.retryable is expected_retryable


@pytest.mark.unit
def test_local_render_deadline_is_408_timeout_error(api_client, env_no_auth) -> None:
    """Local `POST /render` `timeout_ms` expiry returns `408 timeout_error` with non-empty message (`R102`)."""
    with (
        patch(
            "downloader.routes.render.validate_url_async",
            new=AsyncMock(return_value="https://example.com"),
        ),
        patch("downloader.routes.render.asyncio.timeout", side_effect=TimeoutError()),
    ):
        resp = api_client.post(
            "/render",
            json={"url": "https://example.com", "timeout_ms": 1000},
        )
    assert resp.status_code == 408
    detail = resp.json()["detail"]
    assert detail["error_type"] == "timeout_error"
    assert detail["error"].strip() != ""


@pytest.mark.unit
def test_invalid_cron_status_matches_spec(api_client, env_no_auth) -> None:
    """Invalid cron expression on `POST /schedules` returns `400 validation_error` per spec (`R102`)."""
    mock_scheduler = MagicMock()
    app.dependency_overrides[get_scheduler_dependency] = lambda: mock_scheduler
    try:
        resp = api_client.post(
            "/schedules",
            json={
                "name": "Bad Cron",
                "url": "https://example.com/page",
                "cron_expression": "99 25 * * *",
                "format": "text",
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert detail["success"] is False
        assert detail["error_type"] == "validation_error"
        assert "cron" in detail["error"].lower()

        extra_field = api_client.post(
            "/schedules",
            json={
                "name": "Extra Field",
                "url": "https://example.com/page",
                "cron_expression": "0 9 * * *",
                "unknown_extra": True,
            },
        )
        assert extra_field.status_code == 422
    finally:
        app.dependency_overrides.pop(get_scheduler_dependency, None)


@pytest.mark.unit
def test_post_body_validation_returns_400_matching_get_and_contract(
    api_client, env_no_auth
) -> None:
    """`S-5`: Disallowed actions and invalid POST bodies on `/render` and `/batch` return `400`."""
    bad_render = api_client.post(
        "/render",
        json={"url": "https://example.com", "actions": [{"type": "wait", "ms": 3000}]},
    )
    assert bad_render.status_code == 400
    assert bad_render.json()["detail"]["error_type"] == "validation_error"

    bad_batch = api_client.post("/batch", json={"urls": []})
    assert bad_batch.status_code == 400
    assert bad_batch.json()["detail"]["error_type"] == "validation_error"


@pytest.mark.unit
def test_upstream_429_preserves_http_error_type(api_client, env_no_auth) -> None:
    """Upstream 429 from origin is tagged error_type='http_error' so SDK can distinguish it."""
    mock_client = AsyncMock()
    mock_client.download.side_effect = HTTPClientError(
        "HTTP 429: Too Many Requests", status_code=429
    )
    app.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        response = api_client.get("/https://example.com")
        assert response.status_code == 429
        detail = response.json()["detail"]
        assert detail["error_type"] == "http_error"
    finally:
        app.dependency_overrides.pop(get_http_client, None)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ocr_true_with_render_true_runs_ocr_instead_of_browser_render() -> None:
    """`A-14`: `?ocr=true&render=true` checks OCR before the `force_render` branch in single and multi-format."""
    from downloader.models.responses import ResponseMetadata
    from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert
    from downloader.services.multi_format import MultiFormatRequest, process_multiple_formats

    png_bytes = b"\x89PNG\r\n\x1a\nfakepng"
    img_meta: ResponseMetadata = {
        "url": "https://example.com/scan.png",
        "status_code": 200,
        "content_type": "image/png",
        "size": len(png_bytes),
        "headers": {"content-type": "image/png"},
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    mock_renderer = MagicMock()
    mock_renderer.render_html = AsyncMock(return_value=b"<html>wrong branch</html>")

    async def _fetcher() -> tuple[bytes, ResponseMetadata]:
        return png_bytes, img_meta

    deps = ConversionDeps(renderer=mock_renderer, http_fetcher=_fetcher)
    with patch(
        "downloader.services.content_processor.extract_text_from_image",
        new=AsyncMock(return_value="OCR extracted invoice text"),
    ) as mock_ocr:
        single_res = await convert(
            ConversionRequest(
                url="https://example.com/scan.png",
                output_format="text",
                force_render=True,
                force_ocr=True,
            ),
            deps,
        )
        assert single_res.text == "OCR extracted invoice text"
        assert single_res.headers.get("X-OCR-Applied") == "true"
        mock_renderer.render_html.assert_not_awaited()
        assert mock_ocr.await_count == 1

    with patch(
        "downloader.services.content_processor.extract_text_from_image",
        new=AsyncMock(return_value="OCR multi text"),
    ) as mock_multi_ocr:
        multi_res = await process_multiple_formats(
            MultiFormatRequest(
                url="https://example.com/scan.png",
                formats=["text", "markdown"],
                force_render=True,
                force_ocr=True,
            ),
            deps,
        )
        assert multi_res["text/plain"] == "OCR multi text"
        mock_renderer.render_html.assert_not_awaited()
        assert mock_multi_ocr.await_count >= 1

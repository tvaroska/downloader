"""Unit and API tests for Tesseract image-to-text OCR extraction and pixel caps."""

from __future__ import annotations

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytesseract
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from downloader.config import Settings
from downloader.dependencies import (
    get_domain_throttle,
    get_http_client,
    get_robots_checker,
)
from downloader.http_client import HTTPClient
from downloader.main import app
from downloader.models.responses import BatchURLRequest
from downloader.scheduler.executor import ScheduledJobExecutor
from downloader.services.batch_processor import process_single_url_in_batch
from downloader.transformers.ocr import (
    OCRProcessingError,
    OCRUnavailableError,
    extract_text_from_image,
    format_ocr_markdown,
    is_ocr_image_content_type,
    validate_ocr_language,
)


def _make_tiny_png() -> bytes:
    img = Image.new("RGB", (32, 16), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class TestOCRTransformer:
    def test_content_type_and_language_validation(self) -> None:
        assert is_ocr_image_content_type("image/png") is True
        assert is_ocr_image_content_type("image/jpeg; charset=binary") is True
        assert is_ocr_image_content_type("text/html") is False
        assert is_ocr_image_content_type(None) is False

        assert validate_ocr_language("eng+deu", "eng") == "eng+deu"
        assert validate_ocr_language(None, "eng") == "eng"
        with pytest.raises(ValueError, match="Invalid OCR language code"):
            validate_ocr_language("eng; rm -rf /", "eng")

    async def test_extract_text_from_image_and_error_modes(self) -> None:
        png = _make_tiny_png()
        with patch(
            "downloader.transformers.ocr.pytesseract.image_to_string",
            return_value="  Invoice #42\r\n\r\n\r\nTotal: $99  ",
        ) as mock_ocr:
            text = await extract_text_from_image(png, language="eng+fra")
            assert text == "Invoice #42\n\nTotal: $99"
            assert mock_ocr.call_args.kwargs["lang"] == "eng+fra"

        assert "<!-- OCR extracted from https://example.com/a.png -->" in format_ocr_markdown(
            "Hello", "https://example.com/a.png"
        )

        # Missing tesseract binary -> OCRUnavailableError
        with (
            patch(
                "downloader.transformers.ocr.pytesseract.image_to_string",
                side_effect=pytesseract.TesseractNotFoundError(),
            ),
            pytest.raises(OCRUnavailableError),
        ):
            await extract_text_from_image(png)

        # Disabled OCR -> OCRUnavailableError
        disabled_settings = Settings()
        disabled_settings.ocr.enabled = False
        with pytest.raises(OCRUnavailableError, match="disabled"):
            await extract_text_from_image(png, settings=disabled_settings)

        # Corrupt bytes -> OCRProcessingError
        with pytest.raises(OCRProcessingError, match="Invalid or unsupported"):
            await extract_text_from_image(b"not-an-image")

    def test_download_endpoint_ocr_success_and_error_mapping(self) -> None:
        png = _make_tiny_png()
        mock_robots = MagicMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        mock_throttle = MagicMock()
        mock_throttle.wait_if_needed = AsyncMock(return_value=None)
        mock_http = MagicMock()
        mock_http.download = AsyncMock(
            return_value=(
                png,
                {
                    "status_code": 200,
                    "headers": {"content-type": "image/png"},
                    "url": "https://example.com/receipt.png",
                    "size": len(png),
                    "content_type": "image/png",
                    "http_version": "HTTP/1.1",
                    "connection_reused": False,
                },
            )
        )
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
        app.dependency_overrides[get_http_client] = lambda: mock_http

        try:
            with (
                patch(
                    "downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/receipt.png"),
                ),
                patch(
                    "downloader.transformers.ocr.pytesseract.image_to_string",
                    return_value="Receipt Total 19.95",
                ),
                TestClient(app) as client,
            ):
                r_txt = client.get(
                    "/https://example.com/receipt.png?ocr_lang=eng",
                    headers={"Accept": "text/plain"},
                )
                assert r_txt.status_code == 200
                assert r_txt.text == "Receipt Total 19.95"
                assert r_txt.headers["x-ocr-applied"] == "true"
                assert r_txt.headers["x-ocr-language"] == "eng"

                # curl-like Accept: */* with explicit ?ocr=true returns extracted text
                r_curl = client.get(
                    "/https://example.com/receipt.png?ocr=true",
                    headers={"Accept": "*/*"},
                )
                assert r_curl.status_code == 200
                assert r_curl.text == "Receipt Total 19.95"

                r_md = client.get(
                    "/https://example.com/receipt.png",
                    headers={"Accept": "text/markdown"},
                )
                assert r_md.status_code == 200
                assert "Receipt Total 19.95" in r_md.text

            # Missing tesseract binary surfaces cleanly as 503 ocr_unavailable
            with (
                patch(
                    "downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/receipt.png"),
                ),
                patch(
                    "downloader.transformers.ocr.pytesseract.image_to_string",
                    side_effect=pytesseract.TesseractNotFoundError(),
                ),
                TestClient(app) as client,
            ):
                r_503 = client.get(
                    "/https://example.com/receipt.png",
                    headers={"Accept": "text/plain"},
                )
                assert r_503.status_code == 503
                assert r_503.json()["detail"]["error_type"] == "ocr_unavailable"

            # ?ocr=true on HTML origin returns 400 validation_error
            mock_http.download = AsyncMock(
                return_value=(
                    b"<html><body>Not image</body></html>",
                    {
                        "status_code": 200,
                        "headers": {"content-type": "text/html"},
                        "url": "https://example.com/page.html",
                        "size": 35,
                        "content_type": "text/html",
                        "http_version": "HTTP/1.1",
                        "connection_reused": False,
                    },
                )
            )
            with (
                patch(
                    "downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/page.html"),
                ),
                TestClient(app) as client,
            ):
                r_bad = client.get("/https://example.com/page.html?ocr=true")
                assert r_bad.status_code == 400
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_http_client, None)

    async def test_batch_ocr_image_extraction(self) -> None:
        png = _make_tiny_png()
        mock_http = MagicMock()
        mock_http.download = AsyncMock(
            return_value=(
                png,
                {
                    "status_code": 200,
                    "headers": {"content-type": "image/png"},
                    "url": "https://example.com/doc.png",
                    "size": len(png),
                    "content_type": "image/png",
                    "http_version": "HTTP/1.1",
                    "connection_reused": False,
                },
            )
        )
        with (
            patch(
                "downloader.services.batch_processor.validate_url_async",
                new=AsyncMock(return_value="https://example.com/doc.png"),
            ),
            patch(
                "downloader.transformers.ocr.pytesseract.image_to_string",
                return_value="Scanned Batch Doc",
            ),
        ):
            res = await process_single_url_in_batch(
                BatchURLRequest(url="https://example.com/doc.png", format="text"),
                "text",
                10,
                "REQ-OCR",
                mock_http,
                asyncio.Semaphore(2),
            )
            assert res.success is True
            assert res.content == "Scanned Batch Doc"

    async def test_image_origin_html_returns_escaped_ocr_across_single_multi_batch_and_schedule(
        self,
    ) -> None:
        """Image origins requested as `text/html` return HTML-escaped OCR text instead of 415 (`A-3`)."""
        png = _make_tiny_png()

        def _image_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "image/png"},
                stream=httpx.ByteStream(png),
                request=request,
            )

        mock_robots = MagicMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        mock_throttle = MagicMock()
        mock_throttle.wait_if_needed = AsyncMock(return_value=None)

        async with HTTPClient(transport=httpx.MockTransport(_image_handler)) as real_http:
            app.dependency_overrides[get_robots_checker] = lambda: mock_robots
            app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
            app.dependency_overrides[get_http_client] = lambda: real_http
            try:
                with (
                    patch(
                        "downloader.routes.download.validate_url_async",
                        new=AsyncMock(return_value="https://example.com/scan.png"),
                    ),
                    patch(
                        "downloader.services.batch_processor.validate_url_async",
                        new=AsyncMock(return_value="https://example.com/scan.png"),
                    ),
                    patch(
                        "downloader.scheduler.executor.validate_url_async",
                        new=AsyncMock(return_value="https://example.com/scan.png"),
                    ),
                    patch(
                        "downloader.transformers.ocr.pytesseract.image_to_string",
                        return_value="Total <script>alert(1)</script> & $42",
                    ),
                    TestClient(app) as client,
                ):
                    r_html = client.get(
                        "/https://example.com/scan.png",
                        headers={"Accept": "text/html"},
                    )
                    assert r_html.status_code == 200
                    assert r_html.headers["content-type"].startswith("text/html")
                    assert r_html.headers["x-ocr-applied"] == "true"
                    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; $42" in r_html.text
                    assert "<script>" not in r_html.text

                    r_forced = client.get(
                        "/https://example.com/scan.png?ocr=true",
                        headers={"Accept": "text/html"},
                    )
                    assert r_forced.status_code == 200
                    assert r_forced.headers["content-type"].startswith("text/html")
                    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; $42" in r_forced.text

                    r_multi = client.get(
                        "/https://example.com/scan.png",
                        headers={"Accept": "text/html, text/markdown"},
                    )
                    assert r_multi.status_code == 200
                    multi_body = r_multi.json()
                    assert "errors" not in multi_body
                    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in multi_body["text/html"]
                    assert "Total <script>alert(1)</script> & $42" in multi_body["text/markdown"]

                    batch_res = await process_single_url_in_batch(
                        BatchURLRequest(url="https://example.com/scan.png", format="html"),
                        "html",
                        10,
                        "REQ-OCR-HTML",
                        real_http,
                        asyncio.Semaphore(1),
                    )
                    assert batch_res.success is True
                    assert batch_res.content is not None
                    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in batch_res.content

                    mock_storage = MagicMock()
                    mock_storage.store_execution = AsyncMock()
                    sched_exec = ScheduledJobExecutor(
                        http_client=real_http,
                        storage=mock_storage,
                        pdf_semaphore=asyncio.Semaphore(1),
                    )
                    run_rec = await sched_exec.execute(
                        "sched-ocr-html",
                        "https://example.com/scan.png",
                        "html",
                        None,
                    )
                    assert run_rec.success is True
                    assert run_rec.content_size is not None and run_rec.content_size > 0
            finally:
                app.dependency_overrides.pop(get_robots_checker, None)
                app.dependency_overrides.pop(get_domain_throttle, None)
                app.dependency_overrides.pop(get_http_client, None)


@pytest.mark.unit
async def test_pixel_cap_enforced_at_configured_value() -> None:
    """Reject images between 1x and 2x `max_image_pixels` and run OCR on dedicated thread pool (`R50`)."""
    import threading

    # 1,100 x 1,000 = 1,100,000 pixels (> 1,000,000 cap, < 2,000,000 where Pillow alone errors)
    oversized_img = Image.new("RGB", (1100, 1000), color=(255, 255, 255))
    buf = io.BytesIO()
    oversized_img.save(buf, format="PNG")
    oversized_png = buf.getvalue()

    cfg = Settings()
    cfg.ocr.max_image_pixels = 1_000_000

    with (
        patch("downloader.transformers.ocr.pytesseract.image_to_string") as mock_tess,
        pytest.raises(OCRProcessingError, match="exceed"),
    ):
        await extract_text_from_image(oversized_png, settings=cfg)
    mock_tess.assert_not_called()

    # Exact-cap image (1,000 x 1,000 == 1,000,000) succeeds on the dedicated `downloader-ocr` pool
    exact_img = Image.new("RGB", (1000, 1000), color=(255, 255, 255))
    ok_buf = io.BytesIO()
    exact_img.save(ok_buf, format="PNG")

    observed_threads: list[str] = []

    def _fake_ocr(_img: Image.Image, **_kwargs: object) -> str:
        observed_threads.append(threading.current_thread().name)
        return "Bounded OCR OK"

    with patch("downloader.transformers.ocr.pytesseract.image_to_string", side_effect=_fake_ocr):
        out = await extract_text_from_image(ok_buf.getvalue(), settings=cfg)

    assert out == "Bounded OCR OK"
    assert len(observed_threads) == 1
    assert observed_threads[0].startswith("downloader-ocr")

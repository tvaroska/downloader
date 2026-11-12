"""Offline unit & API tests for v0.7.0 OCR image extraction and SSRF-safe webhooks."""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytesseract
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.downloader.config import Settings
from src.downloader.dependencies import (
    get_domain_throttle,
    get_http_client,
    get_job_manager_dependency,
    get_robots_checker,
    get_scheduler_dependency,
)
from src.downloader.job_manager import JobManager
from src.downloader.main import app
from src.downloader.models.responses import BatchRequest, BatchURLRequest
from src.downloader.scheduler.executor import ScheduledJobExecutor
from src.downloader.services.batch_processor import process_single_url_in_batch
from src.downloader.services.webhooks import deliver_webhook, sign_webhook_payload
from src.downloader.transformers.ocr import (
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
            "src.downloader.transformers.ocr.pytesseract.image_to_string",
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
                "src.downloader.transformers.ocr.pytesseract.image_to_string",
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
                    "src.downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/receipt.png"),
                ),
                patch(
                    "src.downloader.transformers.ocr.pytesseract.image_to_string",
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
                    "src.downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/receipt.png"),
                ),
                patch(
                    "src.downloader.transformers.ocr.pytesseract.image_to_string",
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
                    "src.downloader.routes.download.validate_url_async",
                    new=AsyncMock(return_value="https://example.com/page.html"),
                ),
                TestClient(app) as client,
            ):
                r_bad = client.get("/https://example.com/page.html?ocr=true")
                assert r_bad.status_code == 400
        finally:
            app.dependency_overrides.clear()

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
                "src.downloader.routes.batch.validate_url_async",
                new=AsyncMock(return_value="https://example.com/doc.png"),
            ),
            patch(
                "src.downloader.transformers.ocr.pytesseract.image_to_string",
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


class TestWebhookDeliveryAndIntegration:
    def test_hmac_signature_format(self) -> None:
        sig = sign_webhook_payload(b'{"ok":true}', "top-secret")
        assert sig.startswith("sha256=")
        assert len(sig) == len("sha256=") + 64
        assert sig == sign_webhook_payload(b'{"ok":true}', "top-secret")

    async def test_deliver_webhook_signs_retries_and_blocks_ssrf(self) -> None:
        cfg = Settings()
        cfg.webhook.max_retries = 3
        cfg.webhook.backoff_base = 0.0

        # SSRF blocked pre-flight -> 0 attempts
        blocked = await deliver_webhook(
            "http://127.0.0.1:8080/internal",
            "batch.completed",
            {"job_id": "1"},
            settings=cfg,
        )
        assert blocked.delivered is False
        assert blocked.attempts == 0

        # Retries on 500 then succeeds on attempt 2 with valid signature
        attempts: list[httpx.Request] = []

        def _retry_handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(500, text="transient")
            return httpx.Response(202, text="accepted")

        transport = httpx.MockTransport(_retry_handler)
        with patch(
            "src.downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "abc"},
                secret="hook-secret",  # pragma: allowlist secret
                settings=cfg,
                transport=transport,
            )
        assert res.delivered is True
        assert res.attempts == 2
        assert res.status_code == 202
        sent = attempts[-1]
        assert sent.headers["x-downloader-event"] == "batch.completed"
        assert sent.headers["x-downloader-signature-256"] == sign_webhook_payload(
            sent.content, "hook-secret"
        )

        # Deterministic 400 does NOT retry
        calls_400 = 0

        def _bad_handler(_: httpx.Request) -> httpx.Response:
            nonlocal calls_400
            calls_400 += 1
            return httpx.Response(400, text="bad payload")

        with patch(
            "src.downloader.services.webhooks.validate_url_async",
            new=AsyncMock(return_value="https://hooks.example.com/cb"),
        ):
            res_400 = await deliver_webhook(
                "https://hooks.example.com/cb",
                "batch.completed",
                {"job_id": "abc"},
                settings=cfg,
                transport=httpx.MockTransport(_bad_handler),
            )
        assert res_400.delivered is False
        assert res_400.attempts == 1
        assert calls_400 == 1

    async def test_batch_job_manager_fires_webhook_after_results_persisted(self) -> None:
        events: list[str] = []
        jm = JobManager("redis://localhost:6379")

        async def _update_status(job_id: str, status, **kwargs) -> None:
            events.append(f"status:{status.value}")

        async def _store_results(job_id: str, results, summary) -> None:
            events.append("store_results")

        jm.update_job_status = _update_status  # type: ignore[method-assign]
        jm.store_job_results = _store_results  # type: ignore[method-assign]

        async def _fake_processor(job_id: str, req: BatchRequest):
            events.append("process")
            return [{"url": "https://example.com", "success": True}], {"total_requests": 1}

        async def _fake_webhook(webhook_url, event, payload, **kwargs):
            events.append(f"webhook:{event}")

        req = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            webhook_url="https://hooks.example.com/batch",
            webhook_secret="sec",  # pragma: allowlist secret
        )
        with patch("src.downloader.services.batch_processor.deliver_webhook", new=_fake_webhook):
            task = await jm.start_background_job("job-1", _fake_processor, req)
            await task

        assert events == [
            "status:running",
            "process",
            "store_results",
            "status:completed",
            "webhook:batch.completed",
        ]

    async def test_scheduled_executor_screenshot_and_webhook(self) -> None:
        mock_http = MagicMock()
        mock_storage = MagicMock()
        mock_storage.store_execution = AsyncMock()
        executor = ScheduledJobExecutor(
            http_client=mock_http,
            storage=mock_storage,
            pdf_semaphore=asyncio.Semaphore(2),
        )
        with (
            patch(
                "src.downloader.scheduler.executor.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.scheduler.executor.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nSCHED"),
            ),
            patch(
                "src.downloader.scheduler.executor.deliver_webhook",
                new=AsyncMock(),
            ) as mock_hook,
        ):
            execution = await executor.execute(
                "sched-1",
                "https://example.com",
                "screenshot",
                None,
                webhook_url="https://hooks.example.com/sched",
                webhook_secret="shh",  # pragma: allowlist secret
            )
        assert execution.success is True
        assert execution.content_size == len(b"\x89PNG\r\n\x1a\nSCHED")
        mock_hook.assert_awaited_once()
        assert mock_hook.await_args.args[1] == "schedule.completed"
        assert mock_hook.await_args.kwargs["secret"] == "shh"  # pragma: allowlist secret

    def test_schedule_api_validates_webhook_and_hides_secret(self) -> None:
        mock_scheduler = MagicMock()
        fake_job = MagicMock()
        fake_job.id = "sched-abc"
        fake_job.name = "Morning"
        fake_job.next_run_time = None
        fake_job.kwargs = {
            "url": "https://example.com",
            "format": "screenshot",
            "webhook_url": "https://hooks.example.com/ok",
            "webhook_secret": "never-leak-this",  # pragma: allowlist secret
            "created_at": "2026-11-12T00:00:00+00:00",
        }
        fake_job.trigger = "0 9 * * *"
        mock_scheduler.scheduler.add_job = MagicMock(return_value=fake_job)
        mock_scheduler.get_jobs = MagicMock(return_value=[fake_job])
        app.dependency_overrides[get_scheduler_dependency] = lambda: mock_scheduler

        try:
            with TestClient(app) as client:
                # Private loopback webhook rejected at creation time
                bad = client.post(
                    "/schedules",
                    json={
                        "name": "Bad",
                        "url": "https://example.com",
                        "cron_expression": "0 9 * * *",
                        "webhook_url": "http://127.0.0.1/hook",
                    },
                )
                assert bad.status_code == 400

                with patch(
                    "src.downloader.routes.schedules.validate_url_async",
                    new=AsyncMock(
                        side_effect=["https://example.com", "https://hooks.example.com/ok"]
                    ),
                ):
                    ok = client.post(
                        "/schedules",
                        json={
                            "name": "Morning",
                            "url": "https://example.com",
                            "cron_expression": "0 9 * * *",
                            "format": "screenshot",
                            "webhook_url": "https://hooks.example.com/ok",
                            "webhook_secret": "never-leak-this",  # pragma: allowlist secret
                        },
                    )
                assert ok.status_code == 200
                body = ok.json()
                assert body["webhook_url"] == "https://hooks.example.com/ok"
                assert "webhook_secret" not in body
                assert "never-leak-this" not in json.dumps(body)

                listed = client.get("/schedules")
                assert listed.status_code == 200
                assert "never-leak-this" not in listed.text
        finally:
            app.dependency_overrides.clear()

    def test_batch_endpoint_rejects_ssrf_webhook_url(self) -> None:
        mock_jm = MagicMock()
        app.dependency_overrides[get_job_manager_dependency] = lambda: mock_jm
        try:
            with TestClient(app) as client:
                resp = client.post(
                    "/batch",
                    json={
                        "urls": [{"url": "https://example.com"}],
                        "webhook_url": "http://169.254.169.254/latest/meta-data/",
                    },
                )
                assert resp.status_code == 400
                assert resp.json()["detail"]["error_type"] == "validation_error"
        finally:
            app.dependency_overrides.clear()

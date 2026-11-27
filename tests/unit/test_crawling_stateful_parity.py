"""Unit tests for Lua DomainThrottle and stateful `/batch` & `/schedules` crawling parity."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.config import (
    BatchConfig,
    Settings,
)
from downloader.crawling.robots import RobotsChecker, RobotsDisallowedError
from downloader.crawling.throttle import DomainThrottle
from downloader.dependencies import (
    get_job_manager_dependency,
    get_scheduler_dependency,
)
from downloader.main import app
from downloader.middleware import MetricsMiddleware
from downloader.models.responses import BatchRequest, BatchURLRequest
from downloader.pdf_generator import PDFGeneratorError
from downloader.scheduler.executor import ScheduledJobExecutor
from downloader.services.batch_processor import (
    process_background_batch_job,
    process_single_url_in_batch,
)
from downloader.validation import (
    sanitize_custom_headers,
)


@pytest.mark.unit
class TestTrackCSchedulesBatchAndMetrics:
    """Track C & D: schedule validation/crawling/workers, batch progress & headers, and metrics paths."""

    def test_schedule_create_rejects_ssrf_and_prohibited_headers(self) -> None:
        mock_scheduler = MagicMock()
        app.dependency_overrides[get_scheduler_dependency] = lambda: mock_scheduler
        try:
            with TestClient(app) as client:
                ssrf_resp = client.post(
                    "/schedules",
                    json={
                        "name": "Metadata Probe",
                        "url": "http://169.254.169.254/latest/meta-data/",
                        "cron_expression": "0 9 * * *",
                        "format": "text",
                    },
                )
                assert ssrf_resp.status_code == 400
                assert ssrf_resp.json()["detail"]["error_type"] == "validation_error"

                hdr_resp = client.post(
                    "/schedules",
                    json={
                        "name": "Host Spoof",
                        "url": "https://example.com/report",
                        "cron_expression": "0 9 * * *",
                        "format": "text",
                        "headers": {"Host": "internal.local"},
                    },
                )
                assert hdr_resp.status_code == 400
                assert hdr_resp.json()["detail"]["error_type"] == "validation_error"
                mock_scheduler.scheduler.add_job.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_scheduler_dependency, None)

    @pytest.mark.asyncio
    async def test_scheduled_executor_robots_headers_json_size_and_worker_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = AsyncMock()
        http_client = AsyncMock()
        robots = AsyncMock(spec=RobotsChecker)
        throttle = AsyncMock(spec=DomainThrottle)

        # 1) Disallowed by robots.txt fails immediately after attempt 1 without retries
        robots.check_allowed.side_effect = RobotsDisallowedError("https://example.com/private")
        executor = ScheduledJobExecutor(
            http_client=http_client,
            storage=storage,
            robots_checker=robots,
            domain_throttle=throttle,
            max_workers=1,
        )
        disallowed_exec = await executor.execute(
            "sched-1", "https://example.com/private", "text", None
        )
        assert disallowed_exec.success is False
        assert disallowed_exec.attempt == 1
        http_client.download.assert_not_called()

        # 2) Allowed URL forwards custom headers (preserving casing), throttles, and serializes JSON size
        robots.check_allowed.side_effect = None
        robots.check_allowed.return_value = 0.5
        http_client.download.return_value = (
            b'{"hello":"world"}',
            {"url": "https://example.com/api", "content_type": "application/json", "size": 17},
        )
        json_exec = await executor.execute(
            "sched-2",
            "https://example.com/api",
            "json",
            {"X-Custom-Token": "abc-123"},
        )
        assert json_exec.success is True
        assert json_exec.content_size is not None and json_exec.content_size > 40
        throttle.wait_if_needed.assert_awaited_once_with("example.com", 0.5)
        assert http_client.download.await_args.kwargs["headers"] == {"X-Custom-Token": "abc-123"}

        # 3) Single-fetch PDF skips http_client.download
        http_client.download.reset_mock()
        pdf_exec_runner = ScheduledJobExecutor(
            http_client=http_client,
            storage=storage,
            pdf_semaphore=asyncio.Semaphore(1),
            robots_checker=robots,
            domain_throttle=throttle,
            max_workers=1,
        )
        with patch(
            "downloader.pdf_generator.generate_pdf_from_url",
            new_callable=AsyncMock,
            return_value=b"%PDF-1.4 payload",
        ):
            pdf_exec = await pdf_exec_runner.execute(
                "sched-pdf", "https://example.com/doc", "pdf", None
            )
            assert pdf_exec.success is True
            assert pdf_exec.content_size == len(b"%PDF-1.4 payload")
            http_client.download.assert_not_called()

        # 4) Worker semaphore (max_workers=1) is released BEFORE retry backoff sleep
        observed_free_slots_during_sleep: list[int] = []

        async def fake_sleep(_delay: float) -> None:
            observed_free_slots_during_sleep.append(pdf_exec_runner._worker_semaphore._value)

        monkeypatch.setattr("downloader.scheduler.executor.asyncio.sleep", fake_sleep)
        http_client.download.side_effect = RuntimeError("transient upstream failure")
        failed_exec = await pdf_exec_runner.execute(
            "sched-retry", "https://example.com/flaky", "text", None
        )
        assert failed_exec.success is False
        assert failed_exec.attempt == 3
        assert observed_free_slots_during_sleep == [1, 1]

    @pytest.mark.asyncio
    async def test_batch_custom_headers_single_fetch_pdf_and_max_urls_setting(self) -> None:
        sanitized = sanitize_custom_headers({"X-Trace-Id": "trace-42"})
        assert sanitized == {"X-Trace-Id": "trace-42"}

        # Single-fetch PDF in batch skips http_client.download and propagates 404
        mock_http = AsyncMock()
        sem = asyncio.Semaphore(1)
        with patch(
            "downloader.pdf_generator.generate_pdf_from_url",
            new_callable=AsyncMock,
            side_effect=PDFGeneratorError("HTTP 404: Not Found", status_code=404),
        ):
            res_pdf = await process_single_url_in_batch(
                url_request=BatchURLRequest(url="https://example.com/missing", format="pdf"),
                default_format="pdf",
                timeout=10,
                request_id="REQ-PDF",
                http_client=mock_http,
                pdf_semaphore=sem,
            )
            assert res_pdf.success is False
            assert res_pdf.status_code == 404
            assert res_pdf.error_type == "http_error"
            mock_http.download.assert_not_called()

        # Custom headers forwarded on non-PDF batch URL + incremental progress updates
        mock_http.download.return_value = (
            b"<html><body><p>Batch Hello</p></body></html>",
            {"url": "https://example.com", "content_type": "text/html", "size": 44},
        )
        job_manager = AsyncMock()
        job_manager.create_job.return_value = "job-123"
        job_manager.start_background_job = AsyncMock()

        batch_req = BatchRequest(
            urls=[
                BatchURLRequest(
                    url="https://example.com",
                    format="text",
                    custom_headers={"X-Api-Trace": "t1"},
                ),
                BatchURLRequest(url="https://test.com", format="html"),
            ]
        )
        results, summary = await process_background_batch_job(
            job_id="job-123",
            batch_request=batch_req,
            job_manager=job_manager,
            batch_semaphore=asyncio.Semaphore(5),
            http_client=mock_http,
            pdf_semaphore=sem,
            robots_checker=None,
            domain_throttle=None,
        )
        assert summary["successful_requests"] == 2
        assert len(results) == 2
        assert job_manager.update_job_status.call_count == 2
        example_calls = [
            call
            for call in mock_http.download.await_args_list
            if call.args and call.args[0] == "https://example.com"
        ]
        assert len(example_calls) == 1
        assert example_calls[0].kwargs["headers"] == {"X-Api-Trace": "t1"}

        # Configurable BATCH_MAX_URLS_PER_BATCH via app.state.settings + prohibited header 400
        prev_settings = getattr(app.state, "settings", None)
        app.dependency_overrides[get_job_manager_dependency] = lambda: job_manager
        try:
            with TestClient(app) as client:
                app.state.settings = Settings(batch=BatchConfig(max_urls_per_batch=60))
                urls_55 = [{"url": "https://example.com"} for _ in range(55)]
                ok_55 = client.post("/batch", json={"urls": urls_55})
                assert ok_55.status_code == 200

                bad_hdr = client.post(
                    "/batch",
                    json={
                        "urls": [
                            {
                                "url": "https://example.com",
                                "custom_headers": {"Transfer-Encoding": "chunked"},
                            }
                        ]
                    },
                )
                assert bad_hdr.status_code == 400
                assert bad_hdr.json()["detail"]["error_type"] == "validation_error"
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_metrics_middleware_normalize_path_groups_routes(self) -> None:
        mw = MetricsMiddleware(app)
        assert mw._normalize_path("/metrics") == "/metrics"
        assert mw._normalize_path("/metrics/live") == "/metrics/live"
        assert mw._normalize_path("/metrics/unknown") == "/metrics"
        assert mw._normalize_path("/schedules") == "/schedules"
        assert mw._normalize_path("/schedules/1234") == "/schedules/{schedule_id}"
        assert mw._normalize_path("/schedules/1234/history") == "/schedules/{schedule_id}/history"
        assert mw._normalize_path("/docs/oauth2-redirect") == "/docs/oauth2-redirect"
        assert mw._normalize_path("/https://example.com/a/b") == "/download"

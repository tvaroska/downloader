"""Mocked API tests for ethical crawling (robots.txt 403, batch results, opt-out, and metrics)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src.downloader.config import reload_settings
from src.downloader.crawling import DomainThrottle, RobotsChecker
from src.downloader.dependencies import (
    get_domain_throttle,
    get_http_client,
    get_job_manager_dependency,
    get_robots_checker,
)
from src.downloader.main import app
from src.downloader.models.responses import BatchRequest, BatchURLRequest
from src.downloader.routes.batch import process_background_batch_job


def _make_mock_http_client() -> AsyncMock:
    mock_client = AsyncMock()

    async def _download(url: str, priority=None):
        body = f"<html><body><h1>Allowed content from {url}</h1></body></html>".encode()
        return (
            body,
            {
                "url": url,
                "status_code": 200,
                "content_type": "text/html",
                "size": len(body),
                "headers": {"content-type": "text/html"},
            },
        )

    mock_client.download.side_effect = _download
    return mock_client


@pytest.mark.unit
class TestEthicalCrawlingAPI:
    """API-level tests for GET /{url}, POST /batch, RESPECT_ROBOTS_TXT=false, and /metrics."""

    def test_get_url_returns_403_robots_disallowed_and_exposes_metrics(
        self, api_client: TestClient
    ) -> None:
        robots_txt = "User-agent: REST-API-Downloader\nDisallow: /private\nAllow: /public\n"

        async def fetcher(_url: str) -> tuple[int, str]:
            return 200, robots_txt

        checker = RobotsChecker(user_agent="REST-API-Downloader/1.0", fetcher=fetcher)
        throttle = DomainThrottle()
        mock_http = _make_mock_http_client()

        app.dependency_overrides[get_robots_checker] = lambda: checker
        app.dependency_overrides[get_domain_throttle] = lambda: throttle
        app.dependency_overrides[get_http_client] = lambda: mock_http
        try:
            # Disallowed URL -> HTTP 403 with robots_disallowed envelope; upstream download skipped
            blocked = api_client.get("/https://example.com/private/report")
            assert blocked.status_code == 403
            detail = blocked.json()["detail"]
            assert detail["success"] is False
            assert detail["error_type"] == "robots_disallowed"
            assert "REST-API-Downloader" in detail["error"]
            mock_http.download.assert_not_called()

            # Allowed URL on same origin -> hits robots.txt cache and succeeds with 200
            allowed = api_client.get(
                "/https://example.com/public/article",
                headers={"Accept": "text/plain"},
            )
            assert allowed.status_code == 200
            assert "Allowed content" in allowed.text
            mock_http.download.assert_called_once()

            # Prometheus /metrics exposes robots_* counters
            metrics_resp = api_client.get("/metrics")
            assert metrics_resp.status_code == 200
            metrics_text = metrics_resp.text
            assert "robots_evaluations_total" in metrics_text
            assert "robots_cache_hits_total" in metrics_text
            assert "robots_disallowed_total" in metrics_text

            # /metrics/performance also exposes structured crawling counters
            perf = api_client.get("/metrics/performance").json()
            assert perf["crawling"]["robots_evaluations_total"] >= 2
            assert perf["crawling"]["robots_cache_hits_total"] >= 1
            assert perf["crawling"]["robots_disallowed_total"] >= 1
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_http_client, None)

    async def test_batch_records_403_per_disallowed_url_while_allowed_succeed(
        self, api_client: TestClient, mock_job_manager: AsyncMock
    ) -> None:
        robots_txt = "User-agent: *\nDisallow: /blocked\nAllow: /ok\n"

        async def fetcher(_url: str) -> tuple[int, str]:
            return 200, robots_txt

        checker = RobotsChecker(fetcher=fetcher)
        throttle = DomainThrottle()
        mock_http = _make_mock_http_client()

        # Verify HTTP POST /batch submission succeeds when job_manager is available
        app.dependency_overrides[get_job_manager_dependency] = lambda: mock_job_manager
        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: checker
        app.dependency_overrides[get_domain_throttle] = lambda: throttle
        try:
            submit_resp = api_client.post(
                "/batch",
                json={
                    "urls": [
                        {"url": "https://example.com/ok", "format": "text"},
                        {"url": "https://example.com/blocked", "format": "text"},
                    ],
                    "default_format": "text",
                },
            )
            assert submit_resp.status_code == 200
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)

        # Verify background batch execution records per-URL 403 vs 200 outcomes
        batch_req = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example.com/ok", format="text"),
                BatchURLRequest(url="https://example.com/blocked", format="text"),
            ],
            default_format="text",
            concurrency_limit=2,
            timeout_per_url=10,
        )
        results, summary = await process_background_batch_job(
            job_id="job-ethical-1",
            batch_request=batch_req,
            job_manager=mock_job_manager,
            batch_semaphore=asyncio.Semaphore(4),
            http_client=mock_http,
            pdf_semaphore=asyncio.Semaphore(2),
            robots_checker=checker,
            domain_throttle=throttle,
        )

        assert summary["total_requests"] == 2
        assert summary["successful_requests"] == 1
        assert summary["failed_requests"] == 1

        by_url = {item["url"]: item for item in results}
        assert by_url["https://example.com/ok"]["success"] is True
        assert by_url["https://example.com/ok"]["status_code"] == 200

        blocked_item = by_url["https://example.com/blocked"]
        assert blocked_item["success"] is False
        assert blocked_item["status_code"] == 403
        assert blocked_item["error_type"] == "robots_disallowed"

    def test_respect_robots_txt_false_opts_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RESPECT_ROBOTS_TXT", "false")
        monkeypatch.delenv("REDIS_URI", raising=False)
        reload_settings()

        fetcher_called = False

        async def fetcher(_url: str) -> tuple[int, str]:
            nonlocal fetcher_called
            fetcher_called = True
            return 200, "User-agent: *\nDisallow: /\n"

        checker = RobotsChecker(fetcher=fetcher)
        mock_http = _make_mock_http_client()

        app.dependency_overrides[get_robots_checker] = lambda: checker
        app.dependency_overrides[get_http_client] = lambda: mock_http
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                health = client.get("/health").json()
                assert health["configuration"]["respect_robots_txt"] is False

                resp = client.get(
                    "/https://example.com/disallowed-everywhere",
                    headers={"Accept": "text/plain"},
                )
                assert resp.status_code == 200
                assert fetcher_called is False
                mock_http.download.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_http_client, None)
            monkeypatch.delenv("RESPECT_ROBOTS_TXT", raising=False)
            reload_settings()

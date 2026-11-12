"""Offline unit tests for DownloaderClient and AsyncDownloaderClient."""

from __future__ import annotations

import json

import httpx
import pytest
from downloader_sdk import (
    AsyncDownloaderClient,
    BrowserAction,
    DownloaderClient,
    RateLimitError,
    ScheduleCreateRequest,
    ServiceUnavailableError,
    ValidationError,
)


def _mock_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("x-api-key") == "test-key"

    if request.url.path == "/health":
        return httpx.Response(
            200,
            json={
                "status": "healthy",
                "version": "0.7.0",
                "environment": "test",
                "draining": False,
                "active_requests": 0,
            },
        )

    if request.url.path == "/render" and request.method == "POST":
        body = json.loads(request.content)
        assert body["actions"][0]["type"] == "click"
        return httpx.Response(
            200,
            content=b"# Rendered",
            headers={"content-type": "text/markdown; charset=utf-8", "x-rendered-with-js": "true"},
        )

    if request.url.path == "/batch" and request.method == "POST":
        body = json.loads(request.content)
        assert body["webhook_url"] == "https://hooks.example.com/done"
        return httpx.Response(
            200,
            json={
                "job_id": "job-123",
                "status": "pending",
                "created_at": "2026-11-12T00:00:00Z",
                "total_urls": len(body["urls"]),
            },
        )

    if request.url.path == "/jobs/job-123/status":
        return httpx.Response(
            200,
            json={
                "job_id": "job-123",
                "status": "completed",
                "progress": 100,
                "created_at": "2026-11-12T00:00:00Z",
                "total_urls": 1,
                "processed_urls": 1,
                "successful_urls": 1,
                "failed_urls": 0,
                "results_available": True,
            },
        )

    if request.url.path == "/jobs/job-123/results":
        return httpx.Response(
            200,
            json={
                "job_id": "job-123",
                "status": "completed",
                "total_duration": 0.4,
                "results": [{"url": "https://example.com", "success": True}],
                "summary": {"total_requests": 1, "successful_requests": 1},
                "created_at": "2026-11-12T00:00:00Z",
                "completed_at": "2026-11-12T00:00:01Z",
            },
        )

    if request.url.path == "/schedules" and request.method == "POST":
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "sched-1",
                "name": body["name"],
                "url": body["url"],
                "cron_expression": body["cron_expression"],
                "format": body["format"],
                "enabled": True,
                "created_at": "2026-11-12T00:00:00Z",
                "webhook_url": body.get("webhook_url"),
            },
        )

    if (
        request.url.path == "/https://example.com/shot"
        and request.url.params.get("output") == "screenshot"
    ):
        return httpx.Response(
            200, content=b"\x89PNG\r\n\x1a\n", headers={"content-type": "image/png"}
        )

    if request.url.path == "/https://example.com/scan.png":
        assert request.url.params.get("ocr") == "true"
        assert request.url.params.get("ocr_lang") == "eng"
        return httpx.Response(
            200,
            content=b"Invoice #42",
            headers={"content-type": "text/plain; charset=utf-8", "x-ocr-applied": "true"},
        )

    if request.url.path == "/https://rate.limited":
        return httpx.Response(
            429,
            json={"detail": {"error": "Too many requests", "error_type": "rate_limit_exceeded"}},
            headers={"retry-after": "15"},
        )

    if request.url.path == "/https://draining.test":
        return httpx.Response(
            503,
            json={"detail": {"error": "Draining", "error_type": "service_draining"}},
        )

    if request.url.path == "/https://bad.url":
        return httpx.Response(
            400,
            json={"detail": {"error": "Blocked URL", "error_type": "validation_error"}},
        )

    return httpx.Response(200, content=b"Hello World", headers={"content-type": "text/plain"})


def test_sync_client_end_to_end() -> None:
    transport = httpx.MockTransport(_mock_handler)
    with DownloaderClient(
        "http://test.local",
        api_key="test-key",  # pragma: allowlist secret
        transport=transport,
    ) as client:
        health = client.health()
        assert health.version == "0.7.0"
        assert not health.draining

        shot = client.screenshot("https://example.com/shot", full_page=True)
        assert shot.content.startswith(b"\x89PNG")

        ocr_res = client.ocr("https://example.com/scan.png", language="eng")
        assert ocr_res.text == "Invoice #42"
        assert ocr_res.ocr_applied is True

        rendered = client.render(
            "https://example.com",
            output="markdown",
            actions=[BrowserAction(type="click", selector="#go")],
        )
        assert rendered.text == "# Rendered"
        assert rendered.rendered_with_js is True

        job = client.submit_batch(
            ["https://example.com"],
            webhook_url="https://hooks.example.com/done",
        )
        results = client.wait_for_batch(job.job_id, poll_interval=0.01)
        assert results.summary["successful_requests"] == 1

        sched = client.create_schedule(
            ScheduleCreateRequest(
                name="Daily",
                url="https://example.com",
                cron_expression="0 9 * * *",
                format="screenshot",
                webhook_url="https://hooks.example.com/done",
            )
        )
        assert sched.id == "sched-1"
        assert sched.format == "screenshot"

        with pytest.raises(RateLimitError) as rl_err:
            client.download("https://rate.limited")
        assert rl_err.value.retry_after == 15

        with pytest.raises(ServiceUnavailableError):
            client.download("https://draining.test")

        with pytest.raises(ValidationError):
            client.download("https://bad.url")


@pytest.mark.asyncio
async def test_async_client_end_to_end() -> None:
    transport = httpx.MockTransport(_mock_handler)
    async with AsyncDownloaderClient(
        "http://test.local",
        api_key="test-key",  # pragma: allowlist secret
        transport=transport,
    ) as client:
        health = await client.health()
        assert health.status == "healthy"
        res = await client.render(
            "https://example.com",
            output="markdown",
            actions=[BrowserAction(type="click", selector="#go")],
        )
        assert res.text == "# Rendered"
        job = await client.submit_batch(
            ["https://example.com"],
            webhook_url="https://hooks.example.com/done",
        )
        results = await client.wait_for_batch(job.job_id, poll_interval=0.01)
        assert results.job_id == "job-123"

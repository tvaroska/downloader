"""Offline unit tests for DownloaderClient and AsyncDownloaderClient."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from downloader_sdk import (
    APIError,
    AsyncDownloaderClient,
    BrowserAction,
    DownloaderClient,
    RateLimitError,
    ScheduleCreateRequest,
    ScheduleHistory,
    ServiceUnavailableError,
    ValidationError,
)


def _handle_job_route(request: httpx.Request) -> httpx.Response | None:
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
    if request.url.path == "/jobs/job-123" and request.method == "DELETE":
        return httpx.Response(
            200,
            json={"success": True, "message": "Job job-123 cancelled successfully"},
        )
    return None


def _handle_schedule_route(request: httpx.Request) -> httpx.Response | None:
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
    if request.url.path == "/schedules/sched-1" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "id": "sched-1",
                "name": "Daily",
                "url": "https://example.com",
                "cron_expression": "0 9 * * *",
                "format": "screenshot",
                "enabled": True,
                "created_at": "2026-11-12T00:00:00Z",
            },
        )
    if request.url.path == "/schedules/sched-1/history" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "executions": [
                    {
                        "execution_id": "exec-1",
                        "schedule_id": "sched-1",
                        "status": "completed",
                        "started_at": "2026-11-12T09:00:00Z",
                        "completed_at": "2026-11-12T09:00:02Z",
                        "duration": 2.0,
                        "success": True,
                        "content_size": 128,
                        "attempt": 1,
                    }
                ],
                "total": 42,
                "limit": int(request.url.params.get("limit", "20")),
                "offset": int(request.url.params.get("offset", "0")),
            },
        )
    return None


_ERROR_ROUTES: dict[str, tuple[int, dict[str, Any], dict[str, str]]] = {
    "/https://rate.limited": (
        429,
        {"detail": {"error": "Too many requests", "error_type": "rate_limit_exceeded"}},
        {"retry-after": "15"},
    ),
    "/https://draining.test": (
        503,
        {"detail": {"error": "Draining", "error_type": "service_draining"}},
        {},
    ),
    "/https://bad.url": (
        400,
        {"detail": {"error": "Blocked URL", "error_type": "validation_error"}},
        {},
    ),
}


def _handle_download_route(request: httpx.Request) -> httpx.Response:
    if request.url.path in _ERROR_ROUTES:
        status, body, headers = _ERROR_ROUTES[request.url.path]
        return httpx.Response(status, json=body, headers=headers)
    if request.url.path == "/https://example.com/multi":
        accept = request.headers.get("accept", "")
        if "," in accept:
            return httpx.Response(
                200,
                json={
                    "url": "https://example.com/multi",
                    "formats": {"text": "Hello", "markdown": "# Hello"},
                },
            )
        return httpx.Response(
            200,
            content=b"Single format text",
            headers={"content-type": "text/plain; charset=utf-8"},
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
    return httpx.Response(200, content=b"Hello World", headers={"content-type": "text/plain"})


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

    for handler in (_handle_job_route, _handle_schedule_route):
        resp = handler(request)
        if resp is not None:
            return resp

    return _handle_download_route(request)


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

        sched_detail = client.get_schedule("sched-1")
        assert sched_detail.name == "Daily"

        history = client.get_schedule_history("sched-1", limit=10, offset=5)
        assert isinstance(history, ScheduleHistory)
        assert history.total == 42
        assert history.limit == 10
        assert history.offset == 5
        assert len(history.executions) == 1
        assert history.executions[0].execution_id == "exec-1"

        cancel_res = client.cancel_job("job-123")
        assert cancel_res["success"] is True

        multi = client.download_multi("https://example.com/multi", ["text", "markdown"])
        assert multi["formats"]["markdown"] == "# Hello"

        with pytest.raises(APIError) as multi_err:
            client.download_multi("https://example.com/multi", ["text"])
        assert multi_err.value.error_type == "unexpected_content_type"

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

        cancel_res = await client.cancel_job("job-123")
        assert cancel_res["success"] is True

        sched_detail = await client.get_schedule("sched-1")
        assert sched_detail.id == "sched-1"

        history = await client.get_schedule_history("sched-1", limit=15, offset=2)
        assert isinstance(history, ScheduleHistory)
        assert history.total == 42
        assert history.limit == 15
        assert history.offset == 2
        assert history.executions[0].success is True

        multi = await client.download_multi("https://example.com/multi", ["text", "markdown"])
        assert multi["formats"]["text"] == "Hello"

        with pytest.raises(APIError) as multi_err:
            await client.download_multi("https://example.com/multi", ["text"])
        assert multi_err.value.error_type == "unexpected_content_type"


@pytest.mark.asyncio
async def test_download_encodes_target_query() -> None:
    captured_raw_paths: list[bytes] = []
    captured_params: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_raw_paths.append(request.url.raw_path)
        captured_params.append(dict(request.url.params))
        return httpx.Response(200, content=b"encoded-ok", headers={"content-type": "text/plain"})

    target = "https://example.com/search?q=hello%20world&page=2#results"
    with DownloaderClient(
        "http://test.local",
        api_key="test-key",  # pragma: allowlist secret
        transport=httpx.MockTransport(handler),
    ) as sync_client:
        res = sync_client.download(target, render=True)
        assert res.text == "encoded-ok"

    async with AsyncDownloaderClient(
        "http://test.local",
        api_key="test-key",  # pragma: allowlist secret
        transport=httpx.MockTransport(handler),
    ) as async_client:
        ares = await async_client.download(target, wait_for="#main")
        assert ares.text == "encoded-ok"

    assert captured_raw_paths[0] == (
        b"/https://example.com/search%3Fq=hello%2520world&page=2%23results?render=true"
    )
    assert captured_params[0] == {"render": "true"}
    assert captured_raw_paths[1] == (
        b"/https://example.com/search%3Fq=hello%2520world&page=2%23results?wait_for=%23main"
    )
    assert captured_params[1] == {"wait_for": "#main"}

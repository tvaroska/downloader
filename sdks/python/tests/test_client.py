"""Offline unit tests for DownloaderClient and AsyncDownloaderClient."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import httpx
import pytest
from downloader_sdk import (
    APIError,
    AsyncDownloaderClient,
    BatchJobError,
    BrowserAction,
    DownloaderClient,
    RateLimitError,
    ScheduleCreateRequest,
    ScheduleHistory,
    ServiceUnavailableError,
    ValidationError,
    __version__,
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
                "version": "0.8.3",
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
        assert health.version == "0.8.3"
        sdk_manifest = (pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        manifest_match = re.search(r'^version\s*=\s*"([^"]+)"', sdk_manifest, re.MULTILINE)
        assert manifest_match is not None
        assert __version__ == manifest_match.group(1)
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


@pytest.mark.asyncio
async def test_render_forwards_timeout_ms_only_when_specified() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/render"
        payloads.append(json.loads(request.content))
        return httpx.Response(200, content=b"<html/>", headers={"content-type": "text/html"})

    with DownloaderClient("http://test.local", transport=httpx.MockTransport(handler)) as client:
        client.render("https://example.com")
        client.render("https://example.com", timeout_ms=15000)

    async with AsyncDownloaderClient(
        "http://test.local", transport=httpx.MockTransport(handler)
    ) as aclient:
        await aclient.render("https://example.com")
        await aclient.render("https://example.com", timeout_ms=25000)

    assert "timeout_ms" not in payloads[0]
    assert payloads[1]["timeout_ms"] == 15000
    assert "timeout_ms" not in payloads[2]
    assert payloads[3]["timeout_ms"] == 25000


@pytest.mark.asyncio
async def test_path_segments_escaped_and_invalid_segments_rejected() -> None:
    raw_paths: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw_paths.append(request.url.raw_path)
        if request.url.raw_path.endswith(b"/status"):
            return httpx.Response(
                200,
                json={
                    "job_id": "j",
                    "status": "completed",
                    "progress": 100,
                    "created_at": "2026-11-19T00:00:00Z",
                    "total_urls": 1,
                    "processed_urls": 1,
                    "successful_urls": 1,
                    "failed_urls": 0,
                    "results_available": True,
                },
            )
        if request.url.raw_path.endswith(b"/results"):
            return httpx.Response(
                200,
                json={
                    "job_id": "j",
                    "status": "completed",
                    "total_duration": 0.1,
                    "results": [],
                    "summary": {},
                    "created_at": "2026-11-19T00:00:00Z",
                    "completed_at": "2026-11-19T00:00:01Z",
                },
            )
        if b"/history" in request.url.raw_path:
            return httpx.Response(200, json={"executions": [], "total": 0, "limit": 20, "offset": 0})
        if request.method == "DELETE":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200,
            json={
                "id": "s",
                "name": "N",
                "url": "https://example.com",
                "cron_expression": "0 * * * *",
                "format": "text",
                "enabled": True,
                "created_at": "2026-11-19T00:00:00Z",
            },
        )

    tricky = "a/b?c#d%20e"
    encoded = b"a%2Fb%3Fc%23d%2520e"

    with DownloaderClient("http://test.local", transport=httpx.MockTransport(handler)) as client:
        client.get_job_status(tricky)
        client.get_job_results(tricky)
        client.cancel_job(tricky)
        client.get_schedule(tricky)
        client.get_schedule_history(tricky)
        client.delete_schedule(tricky)
        for bad in ("", ".", ".."):
            with pytest.raises(ValueError, match="job_id"):
                client.get_job_status(bad)
            with pytest.raises(ValueError, match="job_id"):
                client.get_job_results(bad)
            with pytest.raises(ValueError, match="job_id"):
                client.cancel_job(bad)
            with pytest.raises(ValueError, match="schedule_id"):
                client.get_schedule(bad)
            with pytest.raises(ValueError, match="schedule_id"):
                client.get_schedule_history(bad)
            with pytest.raises(ValueError, match="schedule_id"):
                client.delete_schedule(bad)

    async with AsyncDownloaderClient(
        "http://test.local", transport=httpx.MockTransport(handler)
    ) as aclient:
        await aclient.get_job_status(tricky)
        await aclient.get_job_results(tricky)
        await aclient.cancel_job(tricky)
        await aclient.get_schedule(tricky)
        await aclient.get_schedule_history(tricky)
        await aclient.delete_schedule(tricky)
        for bad in ("", ".", ".."):
            with pytest.raises(ValueError, match="job_id"):
                await aclient.get_job_status(bad)
            with pytest.raises(ValueError, match="schedule_id"):
                await aclient.delete_schedule(bad)

    expected = [
        b"/jobs/" + encoded + b"/status",
        b"/jobs/" + encoded + b"/results",
        b"/jobs/" + encoded,
        b"/schedules/" + encoded,
        b"/schedules/" + encoded + b"/history?limit=20&offset=0",
        b"/schedules/" + encoded,
    ]
    assert raw_paths == expected + expected


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["cancelled", "failed"])
async def test_wait_for_batch_terminal_error_has_no_synthetic_500(terminal_status: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jobs/job-term/status"
        return httpx.Response(
            200,
            json={
                "job_id": "job-term",
                "status": terminal_status,
                "progress": 50,
                "created_at": "2026-11-19T00:00:00Z",
                "total_urls": 2,
                "processed_urls": 1,
                "successful_urls": 1,
                "failed_urls": 0,
                "error_message": f"Job was {terminal_status}",
                "results_available": False,
            },
        )

    with DownloaderClient("http://test.local", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(BatchJobError) as exc_info:
            client.wait_for_batch("job-term", poll_interval=0.01)
        assert isinstance(exc_info.value, APIError)
        assert exc_info.value.status_code is None
        assert exc_info.value.error_type == f"batch_{terminal_status}"
        assert exc_info.value.job_id == "job-term"
        assert exc_info.value.job_status == terminal_status

    async with AsyncDownloaderClient(
        "http://test.local", transport=httpx.MockTransport(handler)
    ) as aclient:
        with pytest.raises(BatchJobError) as aexc_info:
            await aclient.wait_for_batch("job-term", poll_interval=0.01)
        assert isinstance(aexc_info.value, APIError)
        assert aexc_info.value.status_code is None
        assert aexc_info.value.error_type == f"batch_{terminal_status}"
        assert aexc_info.value.job_status == terminal_status

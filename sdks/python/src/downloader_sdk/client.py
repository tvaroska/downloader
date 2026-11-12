"""Synchronous and asynchronous clients for the REST API Downloader service."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from typing import Any, Literal

import httpx

from .exceptions import (
    APIError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)
from .models import (
    BatchJobResults,
    BatchJobStatus,
    BatchJobSubmission,
    BatchURLItem,
    BrowserAction,
    DownloadResult,
    HealthStatus,
    ScheduleCreateRequest,
    ScheduleExecution,
    ScheduleInfo,
)

_FORMAT_TO_ACCEPT: dict[str, str] = {
    "text": "text/plain",
    "markdown": "text/markdown",
    "html": "text/html",
    "pdf": "application/pdf",
    "screenshot": "image/png",
    "screenshot_jpeg": "image/jpeg",
    "json": "application/json",
    "raw": "*/*",
}


def _build_auth_headers(api_key: str | None) -> dict[str, str]:
    if not api_key:
        return {}
    return {"X-API-Key": api_key}


def _raise_for_api_error(response: httpx.Response) -> None:
    if response.status_code < 400:
        return

    message = f"HTTP {response.status_code}"
    error_type: str | None = None
    try:
        payload = response.json()
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            message = str(detail.get("error") or message)
            error_type = detail.get("error_type")
        elif isinstance(detail, str):
            message = detail
    except Exception:
        if response.text:
            message = response.text[:500]

    retry_after_raw = response.headers.get("retry-after")
    retry_after = int(retry_after_raw) if retry_after_raw and retry_after_raw.isdigit() else None

    if response.status_code in (400, 422):
        raise ValidationError(
            message,
            status_code=response.status_code,
            error_type=error_type,
            retry_after=retry_after,
        )
    if response.status_code == 429:
        raise RateLimitError(
            message,
            status_code=response.status_code,
            error_type=error_type,
            retry_after=retry_after,
        )
    if response.status_code == 503:
        raise ServiceUnavailableError(
            message,
            status_code=response.status_code,
            error_type=error_type,
            retry_after=retry_after,
        )
    raise APIError(
        message,
        status_code=response.status_code,
        error_type=error_type,
        retry_after=retry_after,
    )


def _download_request_args(
    url: str,
    *,
    format: str = "text",
    render: bool = False,
    wait_for: str | None = None,
    output: Literal["screenshot", "pdf"] | None = None,
    full_page: bool = False,
    screenshot_format: Literal["png", "jpeg"] = "png",
    actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ocr: bool = False,
    ocr_lang: str | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    path = f"/{url.lstrip('/')}"
    headers = {"Accept": _FORMAT_TO_ACCEPT.get(format, format)}
    params: dict[str, Any] = {}
    if render:
        params["render"] = "true"
    if wait_for:
        params["wait_for"] = wait_for
    if output is not None:
        params["output"] = output
    if full_page:
        params["full_page"] = "true"
    if screenshot_format != "png":
        params["screenshot_format"] = screenshot_format
    if actions:
        serialized = [
            a.model_dump(exclude_none=True) if isinstance(a, BrowserAction) else dict(a)
            for a in actions
        ]
        params["actions"] = json.dumps(serialized, separators=(",", ":"))
    if ocr:
        params["ocr"] = "true"
    if ocr_lang:
        params["ocr_lang"] = ocr_lang
    return path, headers, params


def _to_download_result(url: str, response: httpx.Response) -> DownloadResult:
    return DownloadResult(
        url=url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type", "application/octet-stream"),
        content=response.content,
        headers={k.lower(): v for k, v in response.headers.items()},
    )


def _serialize_batch_payload(
    urls: Sequence[str | BatchURLItem | dict[str, Any]],
    *,
    default_format: str = "text",
    concurrency_limit: int | None = None,
    timeout_per_url: int | None = None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for entry in urls:
        if isinstance(entry, str):
            items.append({"url": entry})
        elif isinstance(entry, BatchURLItem):
            items.append(entry.model_dump(exclude_none=True))
        else:
            items.append(dict(entry))
    payload: dict[str, Any] = {
        "urls": items,
        "default_format": default_format,
    }
    if concurrency_limit is not None:
        payload["concurrency_limit"] = concurrency_limit
    if timeout_per_url is not None:
        payload["timeout_per_url"] = timeout_per_url
    if webhook_url is not None:
        payload["webhook_url"] = webhook_url
    if webhook_secret is not None:
        payload["webhook_secret"] = webhook_secret
    return payload


class DownloaderClient:
    """Synchronous typed client for the REST API Downloader service."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=_build_auth_headers(api_key),
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> DownloaderClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def health(self) -> HealthStatus:
        resp = self._client.get("/health")
        _raise_for_api_error(resp)
        return HealthStatus.model_validate(resp.json())

    def download(
        self,
        url: str,
        *,
        format: str = "text",
        render: bool = False,
        wait_for: str | None = None,
        output: Literal["screenshot", "pdf"] | None = None,
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
        ocr: bool = False,
        ocr_lang: str | None = None,
    ) -> DownloadResult:
        path, headers, params = _download_request_args(
            url,
            format=format,
            render=render,
            wait_for=wait_for,
            output=output,
            full_page=full_page,
            screenshot_format=screenshot_format,
            actions=actions,
            ocr=ocr,
            ocr_lang=ocr_lang,
        )
        resp = self._client.get(path, headers=headers, params=params)
        _raise_for_api_error(resp)
        return _to_download_result(url, resp)

    def download_multi(
        self,
        url: str,
        formats: Sequence[str],
        *,
        render: bool = False,
        wait_for: str | None = None,
        full_page: bool = False,
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        accept = ", ".join(_FORMAT_TO_ACCEPT.get(f, f) for f in formats)
        path, _, params = _download_request_args(
            url,
            render=render,
            wait_for=wait_for,
            full_page=full_page,
            actions=actions,
        )
        resp = self._client.get(path, headers={"Accept": accept}, params=params)
        _raise_for_api_error(resp)
        return dict(resp.json())

    def screenshot(
        self,
        url: str,
        *,
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ) -> DownloadResult:
        return self.download(
            url,
            output="screenshot",
            full_page=full_page,
            screenshot_format=screenshot_format,
            wait_for=wait_for,
            actions=actions,
        )

    def pdf(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ) -> DownloadResult:
        return self.download(url, output="pdf", wait_for=wait_for, actions=actions)

    def ocr(
        self,
        image_url: str,
        *,
        format: Literal["text", "markdown"] = "text",
        language: str | None = None,
    ) -> DownloadResult:
        return self.download(image_url, format=format, ocr=True, ocr_lang=language)

    def render(
        self,
        url: str,
        *,
        output: Literal["html", "text", "markdown", "pdf", "screenshot"] = "html",
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] = (),
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
    ) -> DownloadResult:
        payload = {
            "url": url,
            "output": output,
            "wait_for": wait_for,
            "actions": [
                a.model_dump(exclude_none=True) if isinstance(a, BrowserAction) else dict(a)
                for a in actions
            ],
            "full_page": full_page,
            "screenshot_format": screenshot_format,
        }
        resp = self._client.post("/render", json=payload)
        _raise_for_api_error(resp)
        return _to_download_result(url, resp)

    def submit_batch(
        self,
        urls: Sequence[str | BatchURLItem | dict[str, Any]],
        *,
        default_format: str = "text",
        concurrency_limit: int | None = None,
        timeout_per_url: int | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> BatchJobSubmission:
        payload = _serialize_batch_payload(
            urls,
            default_format=default_format,
            concurrency_limit=concurrency_limit,
            timeout_per_url=timeout_per_url,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
        resp = self._client.post("/batch", json=payload)
        _raise_for_api_error(resp)
        return BatchJobSubmission.model_validate(resp.json())

    def get_job_status(self, job_id: str) -> BatchJobStatus:
        resp = self._client.get(f"/jobs/{job_id}/status")
        _raise_for_api_error(resp)
        return BatchJobStatus.model_validate(resp.json())

    def get_job_results(self, job_id: str) -> BatchJobResults:
        resp = self._client.get(f"/jobs/{job_id}/results")
        _raise_for_api_error(resp)
        return BatchJobResults.model_validate(resp.json())

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        resp = self._client.delete(f"/jobs/{job_id}")
        _raise_for_api_error(resp)
        return dict(resp.json())

    def wait_for_batch(
        self,
        job_id: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 120.0,
    ) -> BatchJobResults:
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_job_status(job_id)
            if status.status == "completed":
                return self.get_job_results(job_id)
            if status.status in {"failed", "cancelled"}:
                raise APIError(
                    status.error_message or f"Batch job {job_id} ended with status {status.status}",
                    status_code=500,
                    error_type=f"batch_{status.status}",
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for batch job {job_id}")
            time.sleep(poll_interval)

    def create_schedule(self, schedule: ScheduleCreateRequest) -> ScheduleInfo:
        resp = self._client.post("/schedules", json=schedule.model_dump(exclude_none=True))
        _raise_for_api_error(resp)
        return ScheduleInfo.model_validate(resp.json())

    def list_schedules(self) -> list[ScheduleInfo]:
        resp = self._client.get("/schedules")
        _raise_for_api_error(resp)
        data = resp.json()
        return [ScheduleInfo.model_validate(item) for item in data.get("schedules", [])]

    def get_schedule(self, schedule_id: str) -> ScheduleInfo:
        resp = self._client.get(f"/schedules/{schedule_id}")
        _raise_for_api_error(resp)
        return ScheduleInfo.model_validate(resp.json())

    def get_schedule_history(
        self, schedule_id: str, *, limit: int = 20, offset: int = 0
    ) -> list[ScheduleExecution]:
        resp = self._client.get(
            f"/schedules/{schedule_id}/history",
            params={"limit": limit, "offset": offset},
        )
        _raise_for_api_error(resp)
        data = resp.json()
        return [ScheduleExecution.model_validate(item) for item in data.get("executions", [])]

    def delete_schedule(self, schedule_id: str) -> dict[str, Any]:
        resp = self._client.delete(f"/schedules/{schedule_id}")
        _raise_for_api_error(resp)
        return dict(resp.json())


class AsyncDownloaderClient:
    """Asynchronous typed client for the REST API Downloader service."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=_build_auth_headers(api_key),
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> AsyncDownloaderClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> HealthStatus:
        resp = await self._client.get("/health")
        _raise_for_api_error(resp)
        return HealthStatus.model_validate(resp.json())

    async def download(
        self,
        url: str,
        *,
        format: str = "text",
        render: bool = False,
        wait_for: str | None = None,
        output: Literal["screenshot", "pdf"] | None = None,
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
        ocr: bool = False,
        ocr_lang: str | None = None,
    ) -> DownloadResult:
        path, headers, params = _download_request_args(
            url,
            format=format,
            render=render,
            wait_for=wait_for,
            output=output,
            full_page=full_page,
            screenshot_format=screenshot_format,
            actions=actions,
            ocr=ocr,
            ocr_lang=ocr_lang,
        )
        resp = await self._client.get(path, headers=headers, params=params)
        _raise_for_api_error(resp)
        return _to_download_result(url, resp)

    async def screenshot(
        self,
        url: str,
        *,
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ) -> DownloadResult:
        return await self.download(
            url,
            output="screenshot",
            full_page=full_page,
            screenshot_format=screenshot_format,
            wait_for=wait_for,
            actions=actions,
        )

    async def pdf(
        self,
        url: str,
        *,
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] | None = None,
    ) -> DownloadResult:
        return await self.download(url, output="pdf", wait_for=wait_for, actions=actions)

    async def ocr(
        self,
        image_url: str,
        *,
        format: Literal["text", "markdown"] = "text",
        language: str | None = None,
    ) -> DownloadResult:
        return await self.download(image_url, format=format, ocr=True, ocr_lang=language)

    async def render(
        self,
        url: str,
        *,
        output: Literal["html", "text", "markdown", "pdf", "screenshot"] = "html",
        wait_for: str | None = None,
        actions: Sequence[BrowserAction | dict[str, Any]] = (),
        full_page: bool = False,
        screenshot_format: Literal["png", "jpeg"] = "png",
    ) -> DownloadResult:
        payload = {
            "url": url,
            "output": output,
            "wait_for": wait_for,
            "actions": [
                a.model_dump(exclude_none=True) if isinstance(a, BrowserAction) else dict(a)
                for a in actions
            ],
            "full_page": full_page,
            "screenshot_format": screenshot_format,
        }
        resp = await self._client.post("/render", json=payload)
        _raise_for_api_error(resp)
        return _to_download_result(url, resp)

    async def submit_batch(
        self,
        urls: Sequence[str | BatchURLItem | dict[str, Any]],
        *,
        default_format: str = "text",
        concurrency_limit: int | None = None,
        timeout_per_url: int | None = None,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> BatchJobSubmission:
        payload = _serialize_batch_payload(
            urls,
            default_format=default_format,
            concurrency_limit=concurrency_limit,
            timeout_per_url=timeout_per_url,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
        resp = await self._client.post("/batch", json=payload)
        _raise_for_api_error(resp)
        return BatchJobSubmission.model_validate(resp.json())

    async def get_job_status(self, job_id: str) -> BatchJobStatus:
        resp = await self._client.get(f"/jobs/{job_id}/status")
        _raise_for_api_error(resp)
        return BatchJobStatus.model_validate(resp.json())

    async def get_job_results(self, job_id: str) -> BatchJobResults:
        resp = await self._client.get(f"/jobs/{job_id}/results")
        _raise_for_api_error(resp)
        return BatchJobResults.model_validate(resp.json())

    async def wait_for_batch(
        self,
        job_id: str,
        *,
        poll_interval: float = 1.0,
        timeout: float = 120.0,
    ) -> BatchJobResults:
        deadline = time.monotonic() + timeout
        while True:
            status = await self.get_job_status(job_id)
            if status.status == "completed":
                return await self.get_job_results(job_id)
            if status.status in {"failed", "cancelled"}:
                raise APIError(
                    status.error_message or f"Batch job {job_id} ended with status {status.status}",
                    status_code=500,
                    error_type=f"batch_{status.status}",
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for batch job {job_id}")
            await asyncio.sleep(poll_interval)

    async def create_schedule(self, schedule: ScheduleCreateRequest) -> ScheduleInfo:
        resp = await self._client.post("/schedules", json=schedule.model_dump(exclude_none=True))
        _raise_for_api_error(resp)
        return ScheduleInfo.model_validate(resp.json())

    async def list_schedules(self) -> list[ScheduleInfo]:
        resp = await self._client.get("/schedules")
        _raise_for_api_error(resp)
        data = resp.json()
        return [ScheduleInfo.model_validate(item) for item in data.get("schedules", [])]

    async def delete_schedule(self, schedule_id: str) -> dict[str, Any]:
        resp = await self._client.delete(f"/schedules/{schedule_id}")
        _raise_for_api_error(resp)
        return dict(resp.json())

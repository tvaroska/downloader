"""Framework-free Ports and Contracts for browser rendering, batch job storage, and work queues."""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .config import get_settings
from .models.browser_actions import BrowserAction, BrowserActionError

__all__ = [
    "ALLOWED_JOB_TRANSITIONS",
    "BatchQueueEnvelope",
    "BrowserAcquireTimeoutError",
    "BrowserAction",
    "BrowserActionError",
    "BrowserCapacityError",
    "BrowserPoolError",
    "BrowserRenderer",
    "BrowserTaskRequest",
    "BrowserTaskResponse",
    "ContentConversionError",
    "ContentTooLargeError",
    "CrawlDelayExceededError",
    "DownloadError",
    "HTTPClientError",
    "HTTPTimeoutError",
    "JobInfo",
    "JobResult",
    "JobStatus",
    "JobStore",
    "OCRError",
    "OCRProcessingError",
    "OCRUnavailableError",
    "PDFGeneratorError",
    "RenderTimeoutError",
    "RobotsDisallowedError",
    "ScreenshotGeneratorError",
    "SelectorTimeoutError",
    "TERMINAL_JOB_STATUSES",
    "UnsupportedMediaTypeError",
    "WorkQueue",
    "WorkerTimeoutError",
    "enforce_rendered_output_size",
]


class DownloadError(Exception):
    """Base exception for download errors."""


class HTTPClientError(DownloadError):
    """HTTP client related errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HTTPTimeoutError(DownloadError):
    """HTTP timeout errors."""


class ContentTooLargeError(DownloadError):
    """Raised when response or rendered content exceeds max_download_size."""

    def __init__(
        self,
        max_size_or_msg: int | str = 0,
        actual_size: int | None = None,
        *,
        max_size: int | None = None,
    ) -> None:
        if isinstance(max_size_or_msg, str):
            resolved_max = max_size if max_size is not None else 0
            message = max_size_or_msg
        else:
            resolved_max = max_size if max_size is not None else int(max_size_or_msg)
            if actual_size is not None:
                message = (
                    f"Content size ({actual_size} bytes) exceeds maximum allowed size "
                    f"of {resolved_max} bytes"
                )
            else:
                message = f"Content size exceeds maximum allowed size of {resolved_max} bytes"
        super().__init__(message)
        self.max_size: int = resolved_max
        self.actual_size: int | None = actual_size


class UnsupportedMediaTypeError(DownloadError):
    """Raised when an upstream binary Content-Type cannot be converted to text/HTML/markdown."""

    def __init__(
        self,
        content_type: str,
        target_format: str = "text",
        message: str | None = None,
    ) -> None:
        self.content_type = content_type
        self.target_format = target_format
        resolved = message or (
            f"Unsupported binary content type '{content_type}' for {target_format} conversion"
        )
        super().__init__(resolved)


class ContentConversionError(DownloadError):
    """Raised when HTML-to-text/markdown extraction fails instead of silently returning raw HTML."""


class BrowserCapacityError(Exception):
    """Raised when fail-fast browser rendering is requested while all browser slots are busy."""


class BrowserPoolError(Exception):
    """Base error for browser pool operations."""


class BrowserAcquireTimeoutError(BrowserPoolError):
    """Raised when acquiring an idle browser from BrowserPool times out."""


class PDFGeneratorError(Exception):
    """Raised when PDF, screenshot, or browser page capture fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ScreenshotGeneratorError(PDFGeneratorError):
    """Raised when screenshot capture fails."""


class RenderTimeoutError(PDFGeneratorError):
    """Raised when browser page navigation or capture exceeds its timeout."""


class WorkerTimeoutError(TimeoutError):
    """Raised when awaiting a remote split-worker browser RPC reply times out."""


class SelectorTimeoutError(Exception):
    """Raised when `wait_for_selector` times out."""

    def __init__(self, selector: str, timeout_ms: int, message: str | None = None) -> None:
        self.selector = selector
        self.timeout_ms = timeout_ms
        super().__init__(
            message or f"Timeout waiting for selector '{selector}' after {timeout_ms}ms"
        )


class RobotsDisallowedError(Exception):
    """Raised when an origin's robots.txt disallows fetching a URL for the active User-Agent."""

    def __init__(
        self,
        url: str,
        user_agent: str = "REST-API-Downloader/1.0",
        reason: str | None = None,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.reason = reason or f"Disallowed by robots.txt for User-Agent '{user_agent}': {url}"
        super().__init__(self.reason)


class CrawlDelayExceededError(Exception):
    """Raised when queued Crawl-delay wait for an origin exceeds `max_wait_seconds`."""

    def __init__(self, domain: str, retry_after: float) -> None:
        self.domain = domain
        self.retry_after = max(0.001, float(retry_after))
        super().__init__(
            f"Crawl-delay queue wait ({self.retry_after:.1f}s) for '{domain}' exceeds limit; retry later."
        )


class OCRError(Exception):
    """Base error for image OCR extraction failures."""


class OCRUnavailableError(OCRError):
    """Raised when OCR is disabled or the system tesseract binary is missing."""


class OCRProcessingError(OCRError):
    """Raised when an image payload cannot be decoded or OCR fails/times out."""


def enforce_rendered_output_size(
    payload: bytes,
    *,
    max_output_bytes: int | None = None,
    egress_policy: Any = None,
) -> bytes:
    """Reject rendered HTML, PDF, or screenshot outputs exceeding `max_download_size`."""
    policy_settings = getattr(egress_policy, "settings", None)
    cap = (
        max_output_bytes
        if max_output_bytes is not None
        else (
            policy_settings.content.max_download_size
            if policy_settings is not None
            else get_settings().content.max_download_size
        )
    )
    if len(payload) > cap:
        raise ContentTooLargeError(max_size=cap, actual_size=len(payload))
    return payload


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)

ALLOWED_JOB_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset(
        {
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.RUNNING: frozenset(
        {JobStatus.RUNNING, JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class JobInfo(BaseModel):
    """Job information model."""

    job_id: str = Field(..., description="Unique job identifier")
    status: JobStatus = Field(..., description="Current job status")
    created_at: datetime = Field(..., description="Job creation timestamp")
    started_at: datetime | None = Field(None, description="Job start timestamp")
    completed_at: datetime | None = Field(None, description="Job completion timestamp")
    progress: int = Field(0, description="Progress percentage (0-100)")
    total_urls: int = Field(0, description="Total number of URLs to process")
    processed_urls: int = Field(0, description="Number of URLs processed")
    successful_urls: int = Field(0, description="Number of successfully processed URLs")
    failed_urls: int = Field(0, description="Number of failed URLs")
    error_message: str | None = Field(None, description="Error message if job failed")
    request_data: dict[str, Any] = Field(..., description="Original request data")
    results_available: bool = Field(False, description="Whether results are available for download")
    expires_at: datetime | None = Field(None, description="Job expiration timestamp")


class JobResult(BaseModel):
    """Job result model for completed jobs."""

    job_id: str = Field(..., description="Job identifier")
    status: JobStatus = Field(..., description="Final job status")
    total_duration: float = Field(..., description="Total processing time in seconds")
    results: list[dict[str, Any]] = Field(..., description="Individual URL results")
    summary: dict[str, Any] = Field(..., description="Processing summary")
    created_at: datetime = Field(..., description="Job creation timestamp")
    completed_at: datetime = Field(..., description="Job completion timestamp")


@dataclass(frozen=True)
class BatchQueueEnvelope:
    """Dequeued batch job reference along with its raw Redis list entry for acking."""

    job_id: str
    trace_headers: dict[str, str]
    raw_payload: str
    attempt: int = 1


class BrowserTaskRequest(BaseModel):
    """Serializable browser task delegated from stateless API pods to worker pods."""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["render_html", "pdf", "screenshot"]
    url: str
    wait_for_selector: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    full_page: bool = False
    screenshot_format: Literal["png", "jpeg"] = "png"
    trace_headers: dict[str, str] = Field(default_factory=dict)
    deadline: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        """Return True when the task carries a deadline that has already passed."""
        if self.deadline is None:
            return False
        current = time.time() if now is None else now
        return current > self.deadline


class BrowserTaskResponse(BaseModel):
    """Result payload returned from a worker pod over Redis."""

    task_id: str
    success: bool
    content_base64: str | None = None
    error: str | None = None
    error_type: str | None = None
    status_code: int | None = None
    timeout_ms: int | None = None

    def decode_bytes(self) -> bytes:
        if not self.content_base64:
            return b""
        return base64.b64decode(self.content_base64)


@runtime_checkable
class BrowserRenderer(Protocol):
    """Port abstracting local Chromium pool rendering versus remote split-worker browser RPC."""

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        fail_fast: bool = True,
    ) -> bytes | None: ...

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None: ...

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes: ...

    async def render_screenshot(
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes: ...


@runtime_checkable
class WorkQueue(Protocol):
    """Port abstracting split-worker batch and browser task queues."""

    async def enqueue_batch_job(
        self,
        job_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> None: ...

    async def pop_batch_job(self, timeout: float = 2.0) -> BatchQueueEnvelope | None: ...

    async def ack_batch_job(self, raw_payload: str) -> None: ...

    async def requeue_batch_job(self, envelope: BatchQueueEnvelope) -> bool: ...

    async def requeue_processing_jobs(
        self,
        *,
        max_attempts: int | None = None,
        on_dead_letter: Callable[[BatchQueueEnvelope], Awaitable[None]] | None = None,
        include_self: bool = True,
    ) -> int: ...

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse: ...

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None: ...

    async def publish_browser_result(self, response: BrowserTaskResponse) -> None: ...

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 60,
    ) -> None: ...

    async def remove_heartbeat(self, worker_id: str) -> None: ...

    async def has_heartbeat(self, worker_id: str) -> bool: ...

    async def get_queue_depths(self) -> dict[str, int]: ...


@runtime_checkable
class JobStore(Protocol):
    """Port abstracting batch job state persistence and background task lifecycle."""

    async def create_job(self, request_data: dict[str, Any]) -> str: ...

    async def get_job_info(self, job_id: str) -> JobInfo | None: ...

    async def update_job_status(  # noqa: PLR0913
        self,
        job_id: str,
        status: JobStatus,
        progress: int | None = None,
        processed_urls: int | None = None,
        successful_urls: int | None = None,
        failed_urls: int | None = None,
        error_message: str | None = None,
    ) -> bool: ...

    async def complete_job(  # noqa: PLR0913
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
        max_result_bytes: int | None = None,
    ) -> bool: ...

    async def store_job_results(  # noqa: PLR0913
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        status: JobStatus | None = None,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
    ) -> None: ...

    async def get_job_results(self, job_id: str) -> JobResult | None: ...

    async def start_background_job(
        self,
        job_id: str,
        job_processor_func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None]: ...

    async def cancel_job(self, job_id: str) -> bool: ...

    async def wait_for_active_jobs(self, timeout: float) -> bool: ...

"""Hexagonal Ports and Adapters for browser rendering, batch job storage, and work queues."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, runtime_checkable

from . import content_converter as _cc
from . import pdf_generator as _pg
from .browser.actions import BrowserAction, BrowserActionError
from .content_converter import SelectorTimeoutError
from .http_client import ContentTooLargeError
from .job_manager import ALLOWED_JOB_TRANSITIONS, JobInfo, JobResult, JobStatus
from .pdf_generator import PDFGeneratorError, enforce_rendered_output_size
from .validation import EgressPolicy
from .worker_queue import BatchQueueEnvelope, BrowserTaskRequest, BrowserTaskResponse

logger = logging.getLogger(__name__)


class BrowserCapacityError(Exception):
    """Raised when fail-fast browser rendering is requested while all browser slots are busy."""


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


class LocalBrowserRenderer:
    """In-process Playwright adapter enforcing the single `pdf_semaphore` capacity gate."""

    def __init__(
        self,
        pdf_semaphore: asyncio.Semaphore | None = None,
        *,
        egress_policy: EgressPolicy | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self.pdf_semaphore = pdf_semaphore
        self.egress_policy = egress_policy
        self.max_output_bytes = max_output_bytes

    @asynccontextmanager
    async def _acquire_slot(self, url: str, *, fail_fast: bool) -> AsyncIterator[bool]:
        if self.pdf_semaphore is None:
            yield True
            return
        if self.pdf_semaphore.locked():
            if fail_fast:
                logger.warning(f"Browser rendering at capacity, rejecting request for: {url}")
                raise BrowserCapacityError(
                    "Browser rendering service temporarily unavailable. Please try again later."
                )
            logger.info(f"Browser semaphore saturated, skipping auto-render fallback for {url}")
            yield False
            return
        async with self.pdf_semaphore:
            yield True

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        fail_fast: bool = True,
    ) -> bytes | None:
        async with self._acquire_slot(url, fail_fast=fail_fast) as acquired:
            if not acquired:
                return None
            kwargs: dict[str, Any] = {}
            if actions:
                kwargs["actions"] = actions
            if self.egress_policy is not None:
                kwargs["egress_policy"] = self.egress_policy
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _cc.render_html_with_playwright(url, wait_for_selector, **kwargs)

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None:
        async with self._acquire_slot(url, fail_fast=False) as acquired:
            if not acquired:
                return None
            kwargs: dict[str, Any] = {}
            if self.egress_policy is not None:
                kwargs["egress_policy"] = self.egress_policy
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _cc.convert_content_with_playwright_fallback(
                url,
                output_format,
                **kwargs,
            )

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        async with self._acquire_slot(url, fail_fast=True):
            kwargs: dict[str, Any] = {}
            if actions:
                kwargs["actions"] = actions
            if wait_for_selector:
                kwargs["wait_for_selector"] = wait_for_selector
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _pg.generate_pdf_from_url(url, **kwargs)

    async def render_screenshot(
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        async with self._acquire_slot(url, fail_fast=True):
            options: dict[str, Any] = {"type": screenshot_format, "full_page": full_page}
            kwargs: dict[str, Any] = {
                "actions": actions,
                "wait_for_selector": wait_for_selector,
            }
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _pg.generate_screenshot_from_url(url, options, **kwargs)


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

    async def requeue_batch_job(self, envelope: BatchQueueEnvelope) -> None: ...

    async def requeue_processing_jobs(self) -> int: ...

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse: ...

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None: ...

    async def publish_browser_result(
        self,
        response: BrowserTaskResponse,
        *,
        ttl_seconds: int = 120,
    ) -> None: ...

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 30,
    ) -> None: ...

    async def remove_heartbeat(self, worker_id: str) -> None: ...

    async def has_heartbeat(self, worker_id: str) -> bool: ...

    async def list_active_workers(self) -> list[dict[str, Any]]: ...

    async def get_queue_depths(self) -> dict[str, int]: ...


class RemoteBrowserRenderer:
    """Split-worker RPC adapter implementing `BrowserRenderer` over a `WorkQueue`."""

    def __init__(
        self,
        work_queue: WorkQueue,
        *,
        timeout: float = 45.0,
        egress_policy: EgressPolicy | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self.work_queue = work_queue
        self.timeout = timeout
        self.egress_policy = egress_policy
        self.max_output_bytes = max_output_bytes

    async def _dispatch_task(self, req: BrowserTaskRequest) -> bytes:
        resp = await self.work_queue.enqueue_browser_task(req, timeout=self.timeout)
        if not resp.success:
            if resp.error_type == "selector_timeout_error":
                raise SelectorTimeoutError(req.wait_for_selector or "", int(self.timeout * 1000))
            if resp.error_type == "browser_action_error":
                raise BrowserActionError(resp.error or "Remote browser action failed")
            if resp.error_type == "content_too_large":
                raise ContentTooLargeError(resp.error or "Rendered content exceeds size limit")
            raise PDFGeneratorError(
                resp.error or f"Remote worker browser task ({req.kind}) failed",
                status_code=resp.status_code,
            )
        payload = resp.decode_bytes()
        return enforce_rendered_output_size(
            payload,
            max_output_bytes=self.max_output_bytes,
            egress_policy=self.egress_policy,
        )

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        fail_fast: bool = True,
    ) -> bytes | None:
        _ = fail_fast
        return await self._dispatch_task(
            BrowserTaskRequest(
                kind="render_html",
                url=url,
                wait_for_selector=wait_for_selector,
                actions=[a.model_dump() for a in (actions or [])],
            )
        )

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None:
        rendered = await self._dispatch_task(BrowserTaskRequest(kind="render_html", url=url))
        return _cc.convert_content(rendered, "text/html", output_format)

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        return await self._dispatch_task(
            BrowserTaskRequest(
                kind="pdf",
                url=url,
                wait_for_selector=wait_for_selector,
                actions=[a.model_dump() for a in (actions or [])],
            )
        )

    async def render_screenshot(
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        return await self._dispatch_task(
            BrowserTaskRequest(
                kind="screenshot",
                url=url,
                wait_for_selector=wait_for_selector,
                actions=[a.model_dump() for a in (actions or [])],
                full_page=full_page,
                screenshot_format=screenshot_format,
            )
        )


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
    ) -> None: ...

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


class InMemoryJobStore:
    """Shared in-memory `JobStore` adapter for fast deterministic unit and API tests."""

    def __init__(self, *, job_ttl: int = 86400) -> None:
        self.job_ttl = job_ttl
        self.redis_client: Any = None
        self._jobs: dict[str, JobInfo] = {}
        self._results: dict[str, JobResult] = {}
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def create_job(self, request_data: dict[str, Any]) -> str:
        job_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, UTC)
        info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now,
            total_urls=len(request_data.get("urls", [])),
            request_data=request_data,
            expires_at=expires_at,
        )
        async with self._lock:
            self._jobs[job_id] = info
        return job_id

    async def get_job_info(self, job_id: str) -> JobInfo | None:
        async with self._lock:
            existing = self._jobs.get(job_id)
            return existing.model_copy(deep=True) if existing is not None else None

    async def update_job_status(  # noqa: PLR0913
        self,
        job_id: str,
        status: JobStatus,
        progress: int | None = None,
        processed_urls: int | None = None,
        successful_urls: int | None = None,
        failed_urls: int | None = None,
        error_message: str | None = None,
    ) -> None:
        async with self._lock:
            info = self._jobs.get(job_id)
            if info is None:
                return
            if status not in ALLOWED_JOB_TRANSITIONS.get(info.status, frozenset()):
                return
            info.status = status
            now = datetime.now(UTC)
            if status == JobStatus.RUNNING and info.started_at is None:
                info.started_at = now
            elif status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
                info.completed_at = now
                info.results_available = status == JobStatus.COMPLETED
            if progress is not None:
                info.progress = progress
            if processed_urls is not None:
                info.processed_urls = processed_urls
            if successful_urls is not None:
                info.successful_urls = successful_urls
            if failed_urls is not None:
                info.failed_urls = failed_urls
            if error_message is not None:
                info.error_message = error_message

    async def store_job_results(  # noqa: PLR0913
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        status: JobStatus | None = None,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
    ) -> None:
        async with self._lock:
            info = self._jobs.get(job_id)
            if info is None:
                return
            resolved_completed_at = completed_at or info.completed_at or datetime.now(UTC)
            if total_duration is not None:
                duration = total_duration
            elif info.started_at is not None:
                duration = (resolved_completed_at - info.started_at).total_seconds()
            else:
                duration = float(summary.get("total_duration", 0.0))
            self._results[job_id] = JobResult(
                job_id=job_id,
                status=status if status is not None else info.status,
                total_duration=duration,
                results=results,
                summary=summary,
                created_at=info.created_at,
                completed_at=resolved_completed_at,
            )

    async def get_job_results(self, job_id: str) -> JobResult | None:
        async with self._lock:
            res = self._results.get(job_id)
            return res.model_copy(deep=True) if res is not None else None

    async def start_background_job(
        self,
        job_id: str,
        job_processor_func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None]:
        async def _runner() -> None:
            batch_req = args[0] if args and hasattr(args[0], "webhook_url") else None
            webhook_status: JobStatus | None = None
            webhook_summary: dict[str, Any] | None = None
            webhook_error: str | None = None
            try:
                await self.update_job_status(job_id, JobStatus.RUNNING)
                results, summary = await job_processor_func(job_id, *args, **kwargs)
                completed_at = datetime.now(UTC)
                duration = float(summary["total_duration"]) if "total_duration" in summary else None
                await self.store_job_results(
                    job_id,
                    results,
                    summary,
                    status=JobStatus.COMPLETED,
                    total_duration=duration,
                    completed_at=completed_at,
                )
                await self.update_job_status(job_id, JobStatus.COMPLETED)
                webhook_status = JobStatus.COMPLETED
                webhook_summary = summary
            except asyncio.CancelledError:
                await self.update_job_status(job_id, JobStatus.CANCELLED)
                raise
            except Exception as exc:  # noqa: BLE001
                # Isolate background batch job failures so task exceptions become FAILED job state.
                await self.update_job_status(job_id, JobStatus.FAILED, error_message=str(exc))
                webhook_status = JobStatus.FAILED
                webhook_error = str(exc)
            finally:
                self._background_tasks.pop(job_id, None)

            if (
                webhook_status is not None
                and batch_req is not None
                and getattr(batch_req, "webhook_url", None)
            ):
                from .services.batch_processor import notify_batch_webhook

                await notify_batch_webhook(
                    job_id,
                    batch_req,
                    webhook_status,
                    summary=webhook_summary,
                    error=webhook_error,
                )

        task = asyncio.create_task(_runner())
        self._background_tasks[job_id] = task
        return task

    async def cancel_job(self, job_id: str) -> bool:
        task = self._background_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return True

        info = await self.get_job_info(job_id)
        if info is not None and info.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            await self.update_job_status(job_id, JobStatus.CANCELLED)
            return True
        return False

    async def wait_for_active_jobs(self, timeout: float) -> bool:
        pending = [t for t in self._background_tasks.values() if not t.done()]
        if not pending:
            return True
        if timeout <= 0:
            return False
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        return len(still_pending) == 0


class InMemoryWorkQueue:
    """Shared in-memory `WorkQueue` adapter for deterministic split-worker tests."""

    def __init__(self, *, max_remote_browser_calls: int = 8) -> None:
        self._batch_pending: deque[str] = deque()
        self._batch_processing: list[str] = []
        self._batch_event = asyncio.Event()
        self._browser_pending: asyncio.Queue[BrowserTaskRequest] = asyncio.Queue()
        self._browser_results: dict[str, asyncio.Queue[BrowserTaskResponse]] = {}
        self._browser_inflight_semaphore = asyncio.Semaphore(max(1, max_remote_browser_calls))
        self._heartbeats: dict[str, tuple[float, dict[str, Any]]] = {}

    async def enqueue_batch_job(
        self,
        job_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
        attempt: int = 1,
    ) -> None:
        payload = json.dumps(
            {
                "job_id": job_id,
                "enqueued_at": datetime.now(UTC).isoformat(),
                "attempt": attempt,
                "trace_headers": trace_headers or {},
            },
            separators=(",", ":"),
        )
        self._batch_pending.appendleft(payload)
        self._batch_event.set()

    async def pop_batch_job(self, timeout: float = 2.0) -> BatchQueueEnvelope | None:
        if not self._batch_pending:
            self._batch_event.clear()
            try:
                await asyncio.wait_for(self._batch_event.wait(), timeout=max(0.01, timeout))
            except TimeoutError:
                return None
        if not self._batch_pending:
            return None
        raw = self._batch_pending.pop()
        self._batch_processing.insert(0, raw)
        data = json.loads(raw)
        return BatchQueueEnvelope(
            job_id=str(data["job_id"]),
            trace_headers=dict(data.get("trace_headers") or {}),
            raw_payload=raw,
            attempt=int(data.get("attempt", 1)),
        )

    async def ack_batch_job(self, raw_payload: str) -> None:
        if raw_payload in self._batch_processing:
            self._batch_processing.remove(raw_payload)

    async def requeue_batch_job(self, envelope: BatchQueueEnvelope) -> None:
        await self.ack_batch_job(envelope.raw_payload)
        data = json.loads(envelope.raw_payload)
        data["attempt"] = envelope.attempt + 1
        self._batch_pending.appendleft(json.dumps(data, separators=(",", ":")))
        self._batch_event.set()

    async def requeue_processing_jobs(self) -> int:
        moved = 0
        while self._batch_processing:
            raw = self._batch_processing.pop()
            self._batch_pending.appendleft(raw)
            moved += 1
        if moved:
            self._batch_event.set()
        return moved

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse:
        if self._browser_inflight_semaphore.locked():
            raise BrowserCapacityError(
                "Remote browser rendering queue is at capacity. Please try again later."
            )
        async with self._browser_inflight_semaphore:
            if task.deadline is None:
                task = task.model_copy(update={"deadline": time.time() + timeout})
            reply_q = self._browser_results.setdefault(task.task_id, asyncio.Queue(maxsize=1))
            await self._browser_pending.put(task)
            try:
                return await asyncio.wait_for(reply_q.get(), timeout=timeout)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"Remote browser worker timed out after {timeout:.1f}s for task {task.task_id}"
                ) from exc
            finally:
                self._browser_results.pop(task.task_id, None)

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None:
        try:
            return await asyncio.wait_for(self._browser_pending.get(), timeout=max(0.01, timeout))
        except TimeoutError:
            return None

    async def publish_browser_result(
        self,
        response: BrowserTaskResponse,
        *,
        ttl_seconds: int = 120,
    ) -> None:
        _ = ttl_seconds
        reply_q = self._browser_results.setdefault(response.task_id, asyncio.Queue(maxsize=1))
        await reply_q.put(response)

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 30,
    ) -> None:
        expires = time.monotonic() + ttl
        self._heartbeats[worker_id] = (
            expires,
            {
                "worker_id": worker_id,
                "updated_at": datetime.now(UTC).isoformat(),
                **metadata,
            },
        )

    async def remove_heartbeat(self, worker_id: str) -> None:
        self._heartbeats.pop(worker_id, None)

    async def has_heartbeat(self, worker_id: str) -> bool:
        entry = self._heartbeats.get(worker_id)
        if entry is None:
            return False
        if entry[0] <= time.monotonic():
            self._heartbeats.pop(worker_id, None)
            return False
        return True

    async def list_active_workers(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        expired = [wid for wid, (exp, _) in self._heartbeats.items() if exp <= now]
        for wid in expired:
            self._heartbeats.pop(wid, None)
        return [dict(meta) for _, meta in self._heartbeats.values()]

    async def get_queue_depths(self) -> dict[str, int]:
        return {
            "batch_pending": len(self._batch_pending),
            "batch_processing": len(self._batch_processing),
            "browser_pending": self._browser_pending.qsize(),
        }

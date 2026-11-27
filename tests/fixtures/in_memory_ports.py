"""Shared in-memory `JobStore` and `WorkQueue` test adapters (`R95`)."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from downloader.config import get_settings
from downloader.job_manager import _cap_job_result_payload_with_counts
from downloader.models.responses import BatchRequest
from downloader.ports import (
    ALLOWED_JOB_TRANSITIONS,
    BatchQueueEnvelope,
    BrowserCapacityError,
    BrowserTaskRequest,
    BrowserTaskResponse,
    JobInfo,
    JobResult,
    JobStatus,
    JobStore,
    WorkerTimeoutError,
)
from downloader.services.batch_processor import notify_batch_webhook


class InMemoryJobStore:
    """Shared in-memory `JobStore` adapter for fast deterministic unit and API tests."""

    def __init__(self, *, job_ttl: int = 86400) -> None:
        self.job_ttl = job_ttl
        self._jobs: dict[str, JobInfo] = {}
        self._results: dict[str, JobResult] = {}
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._webhook_tasks: set[asyncio.Task[None]] = set()
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

    def _purge_expired_locked(self, job_id: str) -> None:
        info = self._jobs.get(job_id)
        if (
            info is not None
            and info.expires_at is not None
            and datetime.fromtimestamp(time.time(), UTC) >= info.expires_at
        ):
            self._jobs.pop(job_id, None)
            self._results.pop(job_id, None)

    async def get_job_info(self, job_id: str) -> JobInfo | None:
        async with self._lock:
            self._purge_expired_locked(job_id)
            existing = self._jobs.get(job_id)
            return existing.model_copy(deep=True) if existing is not None else None

    async def update_job_status(
        self,
        job_id: str,
        status: JobStatus,
        progress: int | None = None,
        processed_urls: int | None = None,
        successful_urls: int | None = None,
        failed_urls: int | None = None,
        error_message: str | None = None,
    ) -> bool:
        async with self._lock:
            self._purge_expired_locked(job_id)
            info = self._jobs.get(job_id)
            if info is None:
                return False
            if status not in ALLOWED_JOB_TRANSITIONS.get(info.status, frozenset()):
                return False
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
            info.expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, UTC)
            return True

    async def complete_job(
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
        max_result_bytes: int | None = None,
    ) -> bool:
        cap = (
            max_result_bytes
            if max_result_bytes is not None
            else get_settings().content.max_download_size
        )
        async with self._lock:
            self._purge_expired_locked(job_id)
            info = self._jobs.get(job_id)
            if info is None:
                return False
            if JobStatus.COMPLETED not in ALLOWED_JOB_TRANSITIONS.get(info.status, frozenset()):
                return False
            resolved_completed_at = completed_at or datetime.now(UTC)
            if total_duration is not None:
                duration = total_duration
            elif info.started_at is not None:
                duration = (resolved_completed_at - info.started_at).total_seconds()
            else:
                duration = float(summary.get("total_duration", 0.0))
            uncapped = JobResult(
                job_id=job_id,
                status=JobStatus.COMPLETED,
                total_duration=duration,
                results=results,
                summary=summary,
                created_at=info.created_at,
                completed_at=resolved_completed_at,
            )
            capped_json, succ, fail = _cap_job_result_payload_with_counts(uncapped, cap)
            capped_result = JobResult.model_validate_json(capped_json)
            info.status = JobStatus.COMPLETED
            info.completed_at = resolved_completed_at
            info.results_available = True
            info.progress = 100
            info.processed_urls = len(capped_result.results)
            info.successful_urls = succ
            info.failed_urls = fail
            self._results[job_id] = capped_result
            return True

    async def store_job_results(
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        status: JobStatus | None = None,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
        max_result_bytes: int | None = None,
    ) -> None:
        cap = (
            max_result_bytes
            if max_result_bytes is not None
            else get_settings().content.max_download_size
        )
        async with self._lock:
            self._purge_expired_locked(job_id)
            info = self._jobs.get(job_id)
            if info is None or info.status in {JobStatus.CANCELLED, JobStatus.FAILED}:
                return
            resolved_completed_at = completed_at or info.completed_at or datetime.now(UTC)
            if total_duration is not None:
                duration = total_duration
            elif info.started_at is not None:
                duration = (resolved_completed_at - info.started_at).total_seconds()
            else:
                duration = float(summary.get("total_duration", 0.0))
            uncapped = JobResult(
                job_id=job_id,
                status=status if status is not None else info.status,
                total_duration=duration,
                results=results,
                summary=summary,
                created_at=info.created_at,
                completed_at=resolved_completed_at,
            )
            capped_json, _, _ = _cap_job_result_payload_with_counts(uncapped, cap)
            self._results[job_id] = JobResult.model_validate_json(capped_json)

    async def get_job_results(self, job_id: str) -> JobResult | None:
        async with self._lock:
            self._purge_expired_locked(job_id)
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
            current_task = asyncio.current_task()
            batch_req = args[0] if args and hasattr(args[0], "webhook_url") else None
            webhook_status: JobStatus | None = None
            webhook_summary: dict[str, Any] | None = None
            webhook_error: str | None = None
            try:
                if not await self.update_job_status(job_id, JobStatus.RUNNING):
                    return
                results, summary = await job_processor_func(job_id, *args, **kwargs)
                completed_at = datetime.now(UTC)
                duration = float(summary["total_duration"]) if "total_duration" in summary else None
                if await self.complete_job(
                    job_id,
                    results,
                    summary,
                    total_duration=duration,
                    completed_at=completed_at,
                ):
                    webhook_status = JobStatus.COMPLETED
                    webhook_summary = summary
            except asyncio.CancelledError:
                await self.update_job_status(job_id, JobStatus.CANCELLED)
                raise
            except Exception as exc:  # noqa: BLE001
                if await self.update_job_status(job_id, JobStatus.FAILED, error_message=str(exc)):
                    webhook_status = JobStatus.FAILED
                    webhook_error = str(exc)
            finally:
                self._background_tasks.pop(job_id, None)

            if (
                webhook_status is not None
                and batch_req is not None
                and getattr(batch_req, "webhook_url", None)
            ):
                if current_task is not None:
                    self._webhook_tasks.add(current_task)
                try:
                    await notify_batch_webhook(
                        job_id,
                        batch_req,
                        webhook_status,
                        summary=webhook_summary,
                        error=webhook_error,
                    )
                finally:
                    if current_task is not None:
                        self._webhook_tasks.discard(current_task)

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
            return await self.update_job_status(job_id, JobStatus.CANCELLED)
        return False

    async def wait_for_active_jobs(self, timeout: float) -> bool:
        pending = [
            t for t in (*self._background_tasks.values(), *self._webhook_tasks) if not t.done()
        ]
        if not pending:
            return True
        if timeout <= 0:
            return False
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        return len(still_pending) == 0


class InMemoryWorkQueue:
    """Shared in-memory `WorkQueue` adapter for deterministic split-worker tests."""

    def __init__(
        self,
        *,
        max_remote_browser_calls: int = 8,
        max_attempts: int = 3,
        job_store: JobStore | None = None,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self._job_store = job_store
        self._batch_pending: deque[str] = deque()
        self._batch_processing: list[str] = []
        self._batch_event = asyncio.Event()
        self._browser_pending: asyncio.Queue[BrowserTaskRequest] = asyncio.Queue()
        self._browser_results: dict[str, asyncio.Queue[BrowserTaskResponse]] = {}
        self._browser_inflight_semaphore = asyncio.Semaphore(max(1, max_remote_browser_calls))
        self._heartbeats: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _fail_exhausted_job(self, job_id: str) -> None:
        if self._job_store is None:
            return
        msg = f"Batch job exceeded max worker attempts ({self.max_attempts})"
        applied = await self._job_store.update_job_status(
            job_id, JobStatus.FAILED, error_message=msg
        )
        if not applied:
            return
        info = await self._job_store.get_job_info(job_id)
        if info is not None and info.request_data.get("webhook_url"):
            req = BatchRequest.model_validate(
                {k: v for k, v in info.request_data.items() if k != "webhook_secret_enc"}
            )
            await notify_batch_webhook(job_id, req, JobStatus.FAILED, error=msg)

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

    async def requeue_batch_job(self, envelope: BatchQueueEnvelope) -> bool:
        if envelope.raw_payload not in self._batch_processing:
            return False
        self._batch_processing.remove(envelope.raw_payload)
        next_attempt = envelope.attempt + 1
        if next_attempt > self.max_attempts:
            await self._fail_exhausted_job(envelope.job_id)
            return False
        data = json.loads(envelope.raw_payload)
        data["attempt"] = next_attempt
        self._batch_pending.appendleft(json.dumps(data, separators=(",", ":")))
        self._batch_event.set()
        return True

    async def requeue_processing_jobs(
        self,
        *,
        max_attempts: int | None = None,
        on_dead_letter: Callable[[BatchQueueEnvelope], Awaitable[None]] | None = None,
        include_self: bool = True,
    ) -> int:
        if not include_self:
            return 0
        cap = self.max_attempts if max_attempts is None else max_attempts
        moved = 0
        exhausted: list[BatchQueueEnvelope] = []
        while self._batch_processing:
            raw = self._batch_processing.pop()
            data = json.loads(raw)
            next_attempt = int(data.get("attempt", 1)) + 1
            if next_attempt > cap:
                exhausted.append(
                    BatchQueueEnvelope(
                        job_id=str(data["job_id"]),
                        trace_headers=dict(data.get("trace_headers") or {}),
                        raw_payload=raw,
                        attempt=next_attempt,
                    )
                )
                continue
            data["attempt"] = next_attempt
            self._batch_pending.appendleft(json.dumps(data, separators=(",", ":")))
            moved += 1
        if moved:
            self._batch_event.set()
        for env in exhausted:
            if on_dead_letter is not None:
                await on_dead_letter(env)
            else:
                await self._fail_exhausted_job(env.job_id)
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
                raise WorkerTimeoutError(
                    f"Remote browser worker timed out after {timeout:.1f}s for task {task.task_id}"
                ) from exc
            finally:
                self._browser_results.pop(task.task_id, None)

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None:
        try:
            return await asyncio.wait_for(self._browser_pending.get(), timeout=max(0.01, timeout))
        except TimeoutError:
            return None

    async def publish_browser_result(self, response: BrowserTaskResponse) -> None:
        reply_q = self._browser_results.setdefault(response.task_id, asyncio.Queue(maxsize=1))
        await reply_q.put(response)

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 60,
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

    async def get_queue_depths(self) -> dict[str, int]:
        return {
            "batch_pending": len(self._batch_pending),
            "batch_processing": len(self._batch_processing),
            "browser_pending": self._browser_pending.qsize(),
        }

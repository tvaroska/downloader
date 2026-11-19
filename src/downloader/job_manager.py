"""Redis-backed background job manager implementing the `JobStore` port."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as redis
from pydantic import ValidationError

from .config import get_settings
from .ports import (
    ALLOWED_JOB_TRANSITIONS,
    TERMINAL_JOB_STATUSES,
    JobInfo,
    JobResult,
    JobStatus,
)

__all__ = [
    "ALLOWED_JOB_TRANSITIONS",
    "TERMINAL_JOB_STATUSES",
    "JobInfo",
    "JobManager",
    "JobResult",
    "JobStatus",
]

logger = logging.getLogger(__name__)

_REDIS_SOCKET_TIMEOUT_SECONDS = 5.0


def _recompute_summary_counts(
    results: list[dict[str, Any]],
    summary: dict[str, Any],
) -> tuple[int, int]:
    total = len(results)
    successful = sum(1 for item in results if item.get("success"))
    failed = total - successful
    summary["total_requests"] = total
    summary["successful_requests"] = successful
    summary["failed_requests"] = failed
    summary["success_rate"] = (successful / total) * 100 if total else 0.0
    return successful, failed


def _cap_job_result_payload_with_counts(
    job_result: JobResult,
    max_bytes: int,
) -> tuple[str, int, int]:
    """Enforce aggregate payload byte cap in linear time and keep summary counts consistent."""
    payload = job_result.model_dump_json()
    existing_success = sum(1 for item in job_result.results if item.get("success"))
    existing_failed = len(job_result.results) - existing_success
    if len(payload.encode("utf-8")) <= max_bytes:
        return payload, existing_success, existing_failed

    trimmed_results = [dict(item) for item in job_result.results]
    trimmed_summary = dict(job_result.summary)
    entry_sizes = [len(json.dumps(item, default=str).encode("utf-8")) for item in trimmed_results]
    estimated_total = len(payload.encode("utf-8"))
    omitted_msg = f"Batch result omitted: aggregate size exceeded {max_bytes} bytes"
    last_checked_idx = len(trimmed_results)

    for idx in range(len(trimmed_results) - 1, -1, -1):
        entry = trimmed_results[idx]
        if entry.get("content") is None and entry.get("content_base64") is None:
            continue
        old_size = entry_sizes[idx]
        entry["content"] = None
        entry["content_base64"] = None
        entry["size"] = 0
        entry["success"] = False
        entry["error_type"] = "content_too_large"
        entry["status_code"] = 413
        entry["error"] = omitted_msg
        new_size = len(json.dumps(entry, default=str).encode("utf-8"))
        entry_sizes[idx] = new_size
        estimated_total = estimated_total - old_size + new_size
        trimmed_summary["truncated"] = True
        last_checked_idx = idx
        if estimated_total <= max_bytes:
            break

    successful, failed = _recompute_summary_counts(trimmed_results, trimmed_summary)
    candidate = job_result.model_copy(
        update={"results": trimmed_results, "summary": trimmed_summary}
    ).model_dump_json()

    # Exact verification pass in case JSON overhead shifted by a few bytes.
    idx = last_checked_idx - 1
    while len(candidate.encode("utf-8")) > max_bytes and idx >= 0:
        entry = trimmed_results[idx]
        idx -= 1
        if entry.get("content") is None and entry.get("content_base64") is None:
            continue
        entry["content"] = None
        entry["content_base64"] = None
        entry["size"] = 0
        entry["success"] = False
        entry["error_type"] = "content_too_large"
        entry["status_code"] = 413
        entry["error"] = omitted_msg
        trimmed_summary["truncated"] = True
        successful, failed = _recompute_summary_counts(trimmed_results, trimmed_summary)
        candidate = job_result.model_copy(
            update={"results": trimmed_results, "summary": trimmed_summary}
        ).model_dump_json()

    return candidate, successful, failed


class JobManager:
    """Redis-backed `JobStore` adapter with atomic WATCH/MULTI updates and connection pooling."""

    def __init__(
        self,
        redis_url: str,
        job_ttl: int = 3600 * 24,
        max_connections: int = 20,
    ) -> None:
        self.redis_url = redis_url
        self.job_ttl = job_ttl
        self.max_connections = max_connections
        self.redis_client: Any = None
        self.connection_pool: redis.ConnectionPool | None = None
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._webhook_tasks: set[asyncio.Task[None]] = set()
        self._last_health_check: float = 0
        self._health_check_interval = 60

    async def connect(self) -> None:
        """Connect to Redis with connection pooling and verify reachability."""
        try:
            self.connection_pool = redis.ConnectionPool.from_url(
                self.redis_url,
                max_connections=self.max_connections,
                socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
                socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
                retry_on_timeout=True,
                retry_on_error=[redis.BusyLoadingError, redis.ConnectionError],
                health_check_interval=30,
                decode_responses=True,
            )
            self.redis_client = redis.Redis(connection_pool=self.connection_pool)
            await cast(Awaitable[bool], self.redis_client.ping())
            logger.info(
                f"Connected to Redis with connection pool (max_connections={self.max_connections})"
            )
        except (redis.RedisError, OSError, ValueError) as exc:
            logger.error(f"Failed to connect to Redis: {exc}")
            raise

    async def wait_for_active_jobs(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for active background and webhook tasks to drain."""
        pending = [
            task
            for task in (*self._background_tasks.values(), *self._webhook_tasks)
            if not task.done()
        ]
        if not pending:
            return True
        if timeout <= 0:
            return False
        logger.info(
            f"Waiting up to {timeout:.1f}s for {len(pending)} active background/webhook task(s) to drain"
        )
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                f"Background job drain timed out: {len(still_pending)} task(s) still running, "
                f"{len(done)} completed"
            )
            return False
        logger.info(f"Drained {len(done)} background/webhook task(s) cleanly")
        return True

    async def disconnect(self) -> None:
        """Cancel active background and webhook tasks and close the Redis connection pool."""
        try:
            tasks_to_cancel: list[asyncio.Task[None]] = []
            for job_id, task in list(self._background_tasks.items()):
                if not task.done():
                    logger.info(f"Cancelling background task for job {job_id}")
                    task.cancel()
                    tasks_to_cancel.append(task)
            for webhook_task in list(self._webhook_tasks):
                if not webhook_task.done():
                    webhook_task.cancel()
                    tasks_to_cancel.append(webhook_task)
            if tasks_to_cancel:
                await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
            self._background_tasks.clear()
            self._webhook_tasks.clear()
        finally:
            if self.redis_client:
                try:
                    await self.redis_client.close()
                finally:
                    if self.connection_pool:
                        await self.connection_pool.disconnect()
            elif self.connection_pool:
                await self.connection_pool.disconnect()
            logger.info("Disconnected from Redis and closed connection pool")

    def _get_job_key(self, job_id: str) -> str:
        return f"job:{job_id}"

    def _get_result_key(self, job_id: str) -> str:
        return f"job_result:{job_id}"

    async def create_job(self, request_data: dict[str, Any]) -> str:
        """Create and persist a new pending batch job."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        job_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, UTC)
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.PENDING,
            created_at=now,
            total_urls=len(request_data.get("urls", [])),
            request_data=request_data,
            expires_at=expires_at,
        )
        await self.redis_client.setex(
            self._get_job_key(job_id),
            self.job_ttl,
            job_info.model_dump_json(),
        )
        logger.info(f"Created job {job_id} with {job_info.total_urls} URLs")
        return job_id

    async def get_job_info(self, job_id: str) -> JobInfo | None:
        """Fetch job metadata from Redis."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        job_data = await self.redis_client.get(self._get_job_key(job_id))
        if not job_data:
            return None
        try:
            return JobInfo.model_validate_json(job_data)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.error(f"Failed to parse job info for {job_id}: {exc}")
            return None

    def _apply_status_fields(  # noqa: PLR0913
        self,
        job_info: JobInfo,
        status: JobStatus,
        progress: int | None,
        processed_urls: int | None,
        successful_urls: int | None,
        failed_urls: int | None,
        error_message: str | None,
    ) -> None:
        job_info.status = status
        now = datetime.now(UTC)
        if status == JobStatus.RUNNING and job_info.started_at is None:
            job_info.started_at = now
        elif status in TERMINAL_JOB_STATUSES:
            job_info.completed_at = now
            job_info.results_available = status == JobStatus.COMPLETED

        if progress is not None:
            job_info.progress = progress
        if processed_urls is not None:
            job_info.processed_urls = processed_urls
        if successful_urls is not None:
            job_info.successful_urls = successful_urls
        if failed_urls is not None:
            job_info.failed_urls = failed_urls
        if error_message is not None:
            job_info.error_message = error_message

    async def update_job_status(  # noqa: PLR0913
        self,
        job_id: str,
        status: JobStatus,
        progress: int | None = None,
        processed_urls: int | None = None,
        successful_urls: int | None = None,
        failed_urls: int | None = None,
        error_message: str | None = None,
        max_retries: int = 3,
    ) -> bool:
        """Update job status atomically via WATCH/MULTI; return True iff the transition applied."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        job_key = self._get_job_key(job_id)
        for attempt in range(max_retries):
            try:
                async with self.redis_client.pipeline(transaction=True) as pipe:
                    await pipe.watch(job_key)
                    job_data = await self.redis_client.get(job_key)
                    if not job_data:
                        logger.warning(f"Attempted to update non-existent job {job_id}")
                        return False

                    job_info = JobInfo.model_validate_json(job_data)
                    if status not in ALLOWED_JOB_TRANSITIONS.get(job_info.status, frozenset()):
                        logger.debug(
                            f"Ignoring invalid status transition for job {job_id}: "
                            f"{job_info.status.value} -> {status.value}"
                        )
                        return False
                    self._apply_status_fields(
                        job_info,
                        status,
                        progress,
                        processed_urls,
                        successful_urls,
                        failed_urls,
                        error_message,
                    )
                    job_info.expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, UTC)
                    pipe.multi()
                    pipe.setex(job_key, self.job_ttl, job_info.model_dump_json())
                    await pipe.execute()
                    return True
            except redis.WatchError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.01 * (2**attempt))
                    continue
                logger.error(
                    f"Job {job_id} failed to update after {max_retries} attempts due to concurrent modifications"
                )
                raise
            except (redis.RedisError, ValidationError, ValueError, OSError) as exc:
                logger.error(f"Failed to update job {job_id}: {exc}")
                raise
        return False

    @staticmethod
    def _cap_job_result_payload(
        job_result: JobResult,
        max_bytes: int,
    ) -> str:
        payload, _, _ = _cap_job_result_payload_with_counts(job_result, max_bytes)
        return payload

    @staticmethod
    def _resolve_completed_duration(
        initial_info: JobInfo,
        summary: dict[str, Any],
        total_duration: float | None,
        resolved_completed_at: datetime,
    ) -> float:
        if total_duration is not None:
            return total_duration
        if initial_info.started_at is not None:
            return (resolved_completed_at - initial_info.started_at).total_seconds()
        return float(summary.get("total_duration", 0.0))

    async def _commit_completed_job_once(
        self,
        job_id: str,
        payload: str,
        counts: tuple[int, int, int],
        resolved_completed_at: datetime,
    ) -> bool:
        assert self.redis_client is not None
        job_key = self._get_job_key(job_id)
        result_key = self._get_result_key(job_id)
        total_urls, successful_count, failed_count = counts
        async with self.redis_client.pipeline(transaction=True) as pipe:
            await pipe.watch(job_key)
            job_data = await self.redis_client.get(job_key)
            if not job_data:
                return False
            job_info = JobInfo.model_validate_json(job_data)
            if JobStatus.COMPLETED not in ALLOWED_JOB_TRANSITIONS.get(job_info.status, frozenset()):
                logger.info(
                    f"Skipping completion for job {job_id}: status changed to "
                    f"{job_info.status.value}"
                )
                return False

            self._apply_status_fields(
                job_info,
                JobStatus.COMPLETED,
                progress=100,
                processed_urls=total_urls,
                successful_urls=successful_count,
                failed_urls=failed_count,
                error_message=None,
            )
            job_info.completed_at = resolved_completed_at
            job_info.expires_at = datetime.fromtimestamp(time.time() + self.job_ttl, UTC)

            pipe.multi()
            pipe.setex(result_key, self.job_ttl, payload)
            pipe.setex(job_key, self.job_ttl, job_info.model_dump_json())
            await pipe.execute()
            logger.info(f"Completed and stored results for job {job_id}")
            return True

    async def complete_job(  # noqa: PLR0913
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
        *,
        total_duration: float | None = None,
        completed_at: datetime | None = None,
        max_result_bytes: int | None = None,
        max_retries: int = 3,
    ) -> bool:
        """Atomically persist completed results and transition job to COMPLETED in one MULTI."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        initial_info = await self.get_job_info(job_id)
        if not initial_info or JobStatus.COMPLETED not in ALLOWED_JOB_TRANSITIONS.get(
            initial_info.status, frozenset()
        ):
            logger.info(f"Skipping completion for missing or terminal job {job_id}")
            return False

        resolved_completed_at = completed_at or datetime.now(UTC)
        resolved_duration = self._resolve_completed_duration(
            initial_info, summary, total_duration, resolved_completed_at
        )
        job_result = JobResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            total_duration=resolved_duration,
            results=results,
            summary=dict(summary),
            created_at=initial_info.created_at,
            completed_at=resolved_completed_at,
        )
        cap = (
            max_result_bytes
            if max_result_bytes is not None
            else get_settings().content.max_download_size
        )
        payload, successful_count, failed_count = await asyncio.to_thread(
            _cap_job_result_payload_with_counts,
            job_result,
            cap,
        )
        counts = (len(results), successful_count, failed_count)
        for attempt in range(max_retries):
            try:
                return await self._commit_completed_job_once(
                    job_id, payload, counts, resolved_completed_at
                )
            except redis.WatchError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.01 * (2**attempt))
                    continue
                logger.error(
                    f"Job {job_id} failed to complete after {max_retries} attempts due to concurrent modifications"
                )
                raise
            except (redis.RedisError, ValidationError, ValueError, OSError) as exc:
                logger.error(f"Failed to complete job {job_id}: {exc}")
                raise
        return False

    async def store_job_results(  # noqa: PLR0913
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
        """Persist completed batch job results in Redis."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        job_info = await self.get_job_info(job_id)
        if not job_info:
            logger.warning(f"Attempted to store results for non-existent job {job_id}")
            return

        resolved_completed_at = completed_at or job_info.completed_at or datetime.now(UTC)
        if total_duration is not None:
            resolved_duration = total_duration
        elif job_info.started_at is not None:
            resolved_duration = (resolved_completed_at - job_info.started_at).total_seconds()
        else:
            resolved_duration = float(summary.get("total_duration", 0.0))

        job_result = JobResult(
            job_id=job_id,
            status=status if status is not None else job_info.status,
            total_duration=resolved_duration,
            results=results,
            summary=dict(summary),
            created_at=job_info.created_at,
            completed_at=resolved_completed_at,
        )
        cap = (
            max_result_bytes
            if max_result_bytes is not None
            else get_settings().content.max_download_size
        )
        payload, _, _ = await asyncio.to_thread(
            _cap_job_result_payload_with_counts,
            job_result,
            cap,
        )
        await self.redis_client.setex(
            self._get_result_key(job_id),
            self.job_ttl,
            payload,
        )
        logger.info(f"Stored results for job {job_id}")

    async def get_job_results(self, job_id: str) -> JobResult | None:
        """Retrieve completed batch job results from Redis."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        result_data = await self.redis_client.get(self._get_result_key(job_id))
        if not result_data:
            return None
        try:
            return JobResult.model_validate_json(result_data)
        except (ValidationError, ValueError, TypeError) as exc:
            logger.error(f"Failed to parse job results for {job_id}: {exc}")
            return None

    def _track_webhook_task(self, coro: Any) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._webhook_tasks.add(task)
        task.add_done_callback(self._webhook_tasks.discard)
        return task

    async def start_background_job(
        self,
        job_id: str,
        job_processor_func: Any,
        *args: Any,
        **kwargs: Any,
    ) -> asyncio.Task[None]:
        """Launch an asynchronous background task for `job_id`."""

        async def job_wrapper() -> None:
            batch_req = args[0] if args and hasattr(args[0], "webhook_url") else None
            webhook_status: JobStatus | None = None
            webhook_summary: dict[str, Any] | None = None
            webhook_error: str | None = None
            try:
                started = await self.update_job_status(job_id, JobStatus.RUNNING)
                if not started:
                    return
                results, summary = await job_processor_func(job_id, *args, **kwargs)
                completed_at = datetime.now(UTC)
                duration = float(summary["total_duration"]) if "total_duration" in summary else None
                completed = await self.complete_job(
                    job_id,
                    results,
                    summary,
                    total_duration=duration,
                    completed_at=completed_at,
                )
                if completed:
                    webhook_status = JobStatus.COMPLETED
                    webhook_summary = summary
            except asyncio.CancelledError:
                with contextlib.suppress(redis.RedisError, OSError, RuntimeError, ValueError):
                    await self.update_job_status(job_id, JobStatus.CANCELLED)
                raise
            except Exception as exc:  # noqa: BLE001
                # Isolate background task exceptions so any processor failure updates job status cleanly.
                logger.error(f"Job {job_id} failed: {exc}")
                should_notify = True
                try:
                    should_notify = await self.update_job_status(
                        job_id, JobStatus.FAILED, error_message=str(exc)
                    )
                except (redis.RedisError, OSError, RuntimeError, ValueError) as status_exc:
                    logger.error(f"Failed to mark job {job_id} as failed: {status_exc}")
                if should_notify:
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

                webhook_task = self._track_webhook_task(
                    notify_batch_webhook(
                        job_id,
                        batch_req,
                        webhook_status,
                        summary=webhook_summary,
                        error=webhook_error,
                    )
                )
                await webhook_task

        task = asyncio.create_task(job_wrapper())
        self._background_tasks[job_id] = task
        logger.info(f"Started background task for job {job_id}")
        return task

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending or running batch job."""
        task = self._background_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.info(f"Cancelled background task for job {job_id}")
            return True

        job_info = await self.get_job_info(job_id)
        if job_info and job_info.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            return await self.update_job_status(job_id, JobStatus.CANCELLED)
        return False

    def _read_pool_details(self) -> dict[str, Any]:
        if not self.connection_pool:
            return {}
        details: dict[str, Any] = {}
        if hasattr(self.connection_pool, "_created_connections"):
            details["created_connections"] = self.connection_pool._created_connections
        if hasattr(self.connection_pool, "_available_connections"):
            details["available_connections"] = len(self.connection_pool._available_connections)
        if hasattr(self.connection_pool, "_in_use_connections"):
            details["in_use_connections"] = len(self.connection_pool._in_use_connections)
        return details

    async def _check_redis_health(self) -> bool:
        try:
            current_time = asyncio.get_running_loop().time()
            if current_time - self._last_health_check < self._health_check_interval:
                return True
            if not self.redis_client:
                return False
            await cast(Awaitable[bool], self.redis_client.ping())
            self._last_health_check = current_time
            return True
        except (redis.RedisError, OSError, RuntimeError) as exc:
            logger.error(f"Redis health check failed: {exc}")
            return False

    async def get_connection_stats(self) -> dict[str, Any]:
        """Return Redis connection pool statistics for `/metrics/live`."""
        if not self.connection_pool:
            return {"status": "disconnected"}
        try:
            is_healthy = await self._check_redis_health()
            stats: dict[str, Any] = {
                "status": "healthy" if is_healthy else "unhealthy",
                "pool_size": self.connection_pool.max_connections,
                "health_check_interval": self._health_check_interval,
            }
            stats.update(self._read_pool_details())
            return stats
        except (redis.RedisError, OSError, RuntimeError, TypeError) as exc:
            logger.error(f"Failed to get connection stats: {exc}")
            return {"status": "error", "error": str(exc)}

"""Redis-backed background job manager implementing the `JobStore` port."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from enum import Enum
from typing import Any, cast

import redis.asyncio as redis
from pydantic import BaseModel, Field, ValidationError

from .config import get_settings

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


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
        self.redis_client: redis.Redis | None = None
        self.connection_pool: redis.ConnectionPool | None = None
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._last_health_check: float = 0
        self._health_check_interval = 60

    async def connect(self) -> None:
        """Connect to Redis with connection pooling and verify reachability."""
        try:
            self.connection_pool = redis.ConnectionPool.from_url(
                self.redis_url,
                max_connections=self.max_connections,
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
        """Wait up to `timeout` seconds for active background tasks to complete cleanly."""
        pending = [task for task in self._background_tasks.values() if not task.done()]
        if not pending:
            return True
        if timeout <= 0:
            return False
        logger.info(
            f"Waiting up to {timeout:.1f}s for {len(pending)} active background job(s) to drain"
        )
        done, still_pending = await asyncio.wait(pending, timeout=timeout)
        if still_pending:
            logger.warning(
                f"Background job drain timed out: {len(still_pending)} job(s) still running, "
                f"{len(done)} completed"
            )
            return False
        logger.info(f"Drained {len(done)} background job(s) cleanly")
        return True

    async def disconnect(self) -> None:
        """Cancel active background tasks and close the Redis connection pool."""
        for job_id, task in list(self._background_tasks.items()):
            if not task.done():
                logger.info(f"Cancelling background task for job {job_id}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._background_tasks.clear()

        if self.redis_client:
            await self.redis_client.close()
        if self.connection_pool:
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
        elif status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
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
    ) -> None:
        """Update job status atomically using a Redis WATCH/MULTI transaction."""
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
                        return

                    job_info = JobInfo.model_validate_json(job_data)
                    self._apply_status_fields(
                        job_info,
                        status,
                        progress,
                        processed_urls,
                        successful_urls,
                        failed_urls,
                        error_message,
                    )
                    pipe.multi()
                    await pipe.setex(job_key, self.job_ttl, job_info.model_dump_json())
                    await pipe.execute()
                    return
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

    async def store_job_results(
        self,
        job_id: str,
        results: list[dict[str, Any]],
        summary: dict[str, Any],
    ) -> None:
        """Persist completed batch job results in Redis."""
        if not self.redis_client:
            raise RuntimeError("Redis client not connected")

        job_info = await self.get_job_info(job_id)
        if not job_info:
            logger.warning(f"Attempted to store results for non-existent job {job_id}")
            return

        job_result = JobResult(
            job_id=job_id,
            status=job_info.status,
            total_duration=(
                (job_info.completed_at - job_info.started_at).total_seconds()
                if job_info.completed_at and job_info.started_at
                else 0.0
            ),
            results=results,
            summary=summary,
            created_at=job_info.created_at,
            completed_at=job_info.completed_at or datetime.now(UTC),
        )
        await self.redis_client.setex(
            self._get_result_key(job_id),
            self.job_ttl,
            job_result.model_dump_json(),
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
            try:
                await self.update_job_status(job_id, JobStatus.RUNNING)
                results, summary = await job_processor_func(job_id, *args, **kwargs)
                await self.store_job_results(job_id, results, summary)
                await self.update_job_status(job_id, JobStatus.COMPLETED)
                if batch_req is not None and getattr(batch_req, "webhook_url", None):
                    from .services.batch_processor import notify_batch_webhook

                    await notify_batch_webhook(
                        job_id, batch_req, JobStatus.COMPLETED, summary=summary
                    )
            except asyncio.CancelledError:
                await self.update_job_status(job_id, JobStatus.CANCELLED)
                raise
            except Exception as exc:  # noqa: BLE001
                # Isolate background task exceptions so any processor failure updates job status cleanly.
                await self.update_job_status(job_id, JobStatus.FAILED, error_message=str(exc))
                logger.error(f"Job {job_id} failed: {exc}")
                if batch_req is not None and getattr(batch_req, "webhook_url", None):
                    from .services.batch_processor import notify_batch_webhook

                    await notify_batch_webhook(job_id, batch_req, JobStatus.FAILED, error=str(exc))
            finally:
                self._background_tasks.pop(job_id, None)

        task = asyncio.create_task(job_wrapper())
        self._background_tasks[job_id] = task
        logger.info(f"Started background task for job {job_id}")
        return task

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a pending or running batch job."""
        task = self._background_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"Cancelled background task for job {job_id}")
            return True

        job_info = await self.get_job_info(job_id)
        if job_info and job_info.status in {JobStatus.PENDING, JobStatus.RUNNING}:
            await self.update_job_status(job_id, JobStatus.CANCELLED)
            return True
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

    async def cleanup_expired_jobs(self) -> None:
        """Prune finished background task references."""
        if not await self._check_redis_health():
            logger.warning("Skipping cleanup due to Redis health check failure")
            return
        completed = [job_id for job_id, task in self._background_tasks.items() if task.done()]
        for job_id in completed:
            self._background_tasks.pop(job_id, None)


_job_manager: JobManager | None = None


async def get_job_manager() -> JobManager:
    """Get or initialize the global `JobManager` instance."""
    global _job_manager
    if _job_manager is None:
        redis_url = os.getenv("REDIS_URI", "redis://localhost:6379")
        _job_manager = JobManager(redis_url, max_connections=get_settings().redis.max_connections)
        await _job_manager.connect()
    return _job_manager


async def cleanup_job_manager() -> None:
    """Disconnect and clear the global `JobManager`."""
    global _job_manager
    if _job_manager:
        await _job_manager.disconnect()
        _job_manager = None

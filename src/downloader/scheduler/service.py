"""Scheduler service wrapping APScheduler with Redis or in-memory persistence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime
from typing import Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import SchedulerConfig

logger = logging.getLogger(__name__)

_REDIS_JOB_STORE_SOCKET_TIMEOUT = 5.0


def _get_redis_job_store(redis_uri: str) -> Any:
    """Build APScheduler's RedisJobStore from a Redis URI with bounded socket timeouts.

    APScheduler 3.x forwards **connect_args directly to redis.Redis.__init__, which rejects
    a raw `url=` kwarg, so build a ConnectionPool via from_url and pass `connection_pool=`.
    """
    from apscheduler.jobstores.redis import RedisJobStore
    from redis import ConnectionPool

    pool = ConnectionPool.from_url(
        redis_uri,
        socket_timeout=_REDIS_JOB_STORE_SOCKET_TIMEOUT,
        socket_connect_timeout=_REDIS_JOB_STORE_SOCKET_TIMEOUT,
    )
    return RedisJobStore(
        jobs_key="scheduler:jobs",
        run_times_key="scheduler:run_times",
        connection_pool=pool,
    )


class SchedulerService:
    """Service wrapper for APScheduler with Redis or in-memory persistence."""

    def __init__(
        self,
        redis_uri: str | None,
        settings: SchedulerConfig,
        *,
        wakeup_interval: float | None = None,
    ) -> None:
        self.redis_uri = redis_uri
        self.settings = settings
        self._scheduler: AsyncIOScheduler | None = None
        self._started = False
        self._wakeup_interval = (
            wakeup_interval
            if wakeup_interval is not None
            else min(30.0, max(1.0, settings.misfire_grace_time / 2))
        )
        self._wakeup_task: asyncio.Task[None] | None = None
        self._status_cache: tuple[float, dict[str, Any]] | None = None

        jobstores: dict[str, Any] = {}
        if settings.job_store_type == "redis":
            if not redis_uri:
                raise RuntimeError("REDIS_URI is required when scheduler job_store_type is 'redis'")
            jobstores["default"] = _get_redis_job_store(redis_uri)
            logger.info("Scheduler configured with Redis job store")
        else:
            jobstores["default"] = MemoryJobStore()
            logger.info("Scheduler configured with in-memory job store")

        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors={"default": AsyncIOExecutor()},
            job_defaults={
                "coalesce": settings.coalesce,
                "max_instances": 1,
                "misfire_grace_time": settings.misfire_grace_time,
            },
            timezone="UTC",
        )
        logger.info(
            f"Scheduler service initialized (job_store={settings.job_store_type}, "
            f"max_workers={settings.max_workers}, misfire_grace={settings.misfire_grace_time}s)"
        )

    @property
    def scheduler(self) -> AsyncIOScheduler:
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized")
        return self._scheduler

    def invalidate_status_cache(self) -> None:
        self._status_cache = None

    async def _periodic_wakeup_loop(self) -> None:
        """Wake APScheduler periodically so jobs added by paused API pods are discovered."""
        try:
            while self._started and self._scheduler is not None:
                await asyncio.sleep(self._wakeup_interval)
                if self._started and self._scheduler is not None and self._scheduler.running:
                    self._scheduler.wakeup()
        except asyncio.CancelledError:
            pass

    async def start(self, *, paused: bool = False) -> None:
        """Start the scheduler (optionally paused for CRUD-only API pods)."""
        if self._started:
            logger.warning("Scheduler already started")
            return
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized")
        self._scheduler.start(paused=paused)
        self._started = True
        self._status_cache = None
        if not paused and self._wakeup_interval > 0:
            self._wakeup_task = asyncio.create_task(self._periodic_wakeup_loop())
        jobs = await asyncio.to_thread(self._scheduler.get_jobs)
        mode_label = "paused (CRUD-only)" if paused else "active"
        logger.info(f"Scheduler started in {mode_label} mode with {len(jobs)} existing jobs")

    async def shutdown(self, wait: bool = True) -> None:
        """Shut down the scheduler cleanly."""
        if not self._started or self._scheduler is None:
            return
        self._started = False
        self._status_cache = None
        if self._wakeup_task is not None:
            self._wakeup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wakeup_task
            self._wakeup_task = None
        await asyncio.to_thread(self._scheduler.shutdown, wait=wait)
        logger.info("Scheduler shutdown complete")

    def is_running(self) -> bool:
        return self._started and self._scheduler is not None and bool(self._scheduler.running)

    async def get_status(self) -> dict[str, Any]:
        if not self._scheduler:
            return {"status": "not_initialized"}
        if not self._started:
            return {"status": "stopped"}

        jobs = await asyncio.to_thread(self._scheduler.get_jobs)
        pending_jobs = [job for job in jobs if job.next_run_time is not None]
        next_run: datetime | None = (
            min(job.next_run_time for job in pending_jobs if job.next_run_time)
            if pending_jobs
            else None
        )
        status = {
            "status": "running" if self._scheduler.running else "paused",
            "job_store_type": self.settings.job_store_type,
            "total_jobs": len(jobs),
            "pending_jobs": len(pending_jobs),
            "next_run_time": next_run.isoformat() if next_run else None,
        }
        self._status_cache = (time.monotonic(), dict(status))
        return status

    async def get_cached_status(self, max_age: float = 15.0) -> dict[str, Any]:
        """Return a bounded-TTL cached status snapshot so unauthenticated probes avoid Redis load."""
        if not self._scheduler:
            return {"status": "not_initialized"}
        if not self._started:
            return {"status": "stopped"}
        if self._status_cache is not None and (time.monotonic() - self._status_cache[0]) < max_age:
            return dict(self._status_cache[1])
        return await self.get_status()

    def get_jobs(self) -> list[Any]:
        return self._scheduler.get_jobs() if self._scheduler else []

    def get_job(self, job_id: str) -> Any | None:
        return self._scheduler.get_job(job_id) if self._scheduler else None

    def remove_job(self, job_id: str) -> None:
        if self._scheduler:
            self._scheduler.remove_job(job_id)
            self._status_cache = None
            logger.info(f"Removed scheduled job: {job_id}")

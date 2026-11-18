"""Scheduler service wrapping APScheduler with Redis or in-memory persistence."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import redis.exceptions as redis_exc
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import SchedulerConfig

logger = logging.getLogger(__name__)


def _get_redis_job_store(redis_uri: str) -> Any:
    """Build APScheduler's RedisJobStore from a Redis URI.

    APScheduler 3.x forwards **connect_args directly to redis.Redis.__init__, which rejects
    a raw `url=` kwarg, so build a ConnectionPool via from_url and pass `connection_pool=`.
    """
    from apscheduler.jobstores.redis import RedisJobStore
    from redis import ConnectionPool

    pool = ConnectionPool.from_url(redis_uri)
    return RedisJobStore(
        jobs_key="scheduler:jobs",
        run_times_key="scheduler:run_times",
        connection_pool=pool,
    )


class SchedulerService:
    """Service wrapper for APScheduler with Redis or in-memory persistence."""

    def __init__(self, redis_uri: str | None, settings: SchedulerConfig) -> None:
        self.redis_uri = redis_uri
        self.settings = settings
        self._scheduler: AsyncIOScheduler | None = None
        self._started = False

        jobstores: dict[str, Any] = {}
        if redis_uri and settings.job_store_type == "redis":
            try:
                jobstores["default"] = _get_redis_job_store(redis_uri)
                logger.info("Scheduler configured with Redis job store")
            except (ImportError, redis_exc.RedisError, OSError, ValueError) as exc:
                logger.warning(
                    f"Failed to configure Redis job store: {exc}. Falling back to memory."
                )
                jobstores["default"] = MemoryJobStore()
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

    async def start(self, *, paused: bool = False) -> None:
        """Start the scheduler (optionally paused for CRUD-only API pods)."""
        if self._started:
            logger.warning("Scheduler already started")
            return
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized")
        self._scheduler.start(paused=paused)
        self._started = True
        jobs = self._scheduler.get_jobs()
        mode_label = "paused (CRUD-only)" if paused else "active"
        logger.info(f"Scheduler started in {mode_label} mode with {len(jobs)} existing jobs")

    async def shutdown(self, wait: bool = True) -> None:
        """Shut down the scheduler cleanly."""
        if not self._started or self._scheduler is None:
            return
        self._scheduler.shutdown(wait=wait)
        self._started = False
        logger.info("Scheduler shutdown complete")

    def is_running(self) -> bool:
        return self._started and self._scheduler is not None and bool(self._scheduler.running)

    async def get_status(self) -> dict[str, Any]:
        if not self._scheduler:
            return {"status": "not_initialized"}
        if not self._started:
            return {"status": "stopped"}

        jobs = self._scheduler.get_jobs()
        pending_jobs = [job for job in jobs if job.next_run_time is not None]
        next_run: datetime | None = (
            min(job.next_run_time for job in pending_jobs if job.next_run_time)
            if pending_jobs
            else None
        )
        return {
            "status": "running" if self._scheduler.running else "paused",
            "job_store_type": self.settings.job_store_type,
            "total_jobs": len(jobs),
            "pending_jobs": len(pending_jobs),
            "next_run_time": next_run.isoformat() if next_run else None,
        }

    def get_jobs(self) -> list[Any]:
        return self._scheduler.get_jobs() if self._scheduler else []

    def get_job(self, job_id: str) -> Any | None:
        return self._scheduler.get_job(job_id) if self._scheduler else None

    def remove_job(self, job_id: str) -> None:
        if self._scheduler:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed scheduled job: {job_id}")

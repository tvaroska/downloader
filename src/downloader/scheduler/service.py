"""Scheduler service for managing scheduled download jobs.

This module provides a wrapper around APScheduler that integrates with
the application's Redis backend and lifecycle management.
"""

import logging
from datetime import datetime

# TYPE_CHECKING import to avoid circular imports
from typing import TYPE_CHECKING, Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import SchedulerConfig

if TYPE_CHECKING:
    from .executor import ScheduledJobExecutor

logger = logging.getLogger(__name__)


# Conditional import for Redis job store
def _get_redis_job_store(redis_uri: str):
    """Get Redis job store with the given URI.

    APScheduler's Redis job store uses synchronous redis-py by default,
    so we configure it with the provided URI.
    """
    from apscheduler.jobstores.redis import RedisJobStore

    return RedisJobStore(
        url=redis_uri, jobs_key="scheduler:jobs", run_times_key="scheduler:run_times"
    )


class SchedulerService:
    """Service wrapper for APScheduler with Redis persistence.

    This class provides lifecycle management for the scheduler, integrating
    with the application's startup/shutdown sequence. It supports both
    Redis-backed persistence (for production) and in-memory storage (for testing).

    Attributes:
        scheduler: The underlying APScheduler instance
        redis_uri: Redis connection URI (None for in-memory mode)
        settings: Scheduler configuration settings
    """

    def __init__(
        self,
        redis_uri: str | None,
        settings: SchedulerConfig,
    ) -> None:
        """Initialize the scheduler service.

        Args:
            redis_uri: Redis connection URI for job persistence.
                      If None, uses in-memory storage.
            settings: Scheduler configuration settings.
        """
        self.redis_uri = redis_uri
        self.settings = settings
        self._scheduler: AsyncIOScheduler | None = None
        self._started = False
        self._executor: ScheduledJobExecutor | None = None

        # Configure job stores
        jobstores: dict[str, Any] = {}
        if redis_uri and settings.job_store_type == "redis":
            try:
                jobstores["default"] = _get_redis_job_store(redis_uri)
                logger.info("Scheduler configured with Redis job store")
            except Exception as e:
                logger.warning(f"Failed to configure Redis job store: {e}. Falling back to memory.")
                jobstores["default"] = MemoryJobStore()
        else:
            jobstores["default"] = MemoryJobStore()
            logger.info("Scheduler configured with in-memory job store")

        # Configure executors
        executors = {
            "default": AsyncIOExecutor(),
        }

        # Configure job defaults
        job_defaults = {
            "coalesce": settings.coalesce,
            "max_instances": 1,
            "misfire_grace_time": settings.misfire_grace_time,
        }

        # Create scheduler
        self._scheduler = AsyncIOScheduler(
            jobstores=jobstores,
            executors=executors,
            job_defaults=job_defaults,
            timezone="UTC",
        )

        logger.info(
            f"Scheduler service initialized (job_store={settings.job_store_type}, "
            f"max_workers={settings.max_workers}, misfire_grace={settings.misfire_grace_time}s)"
        )

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """Get the underlying APScheduler instance.

        Returns:
            The AsyncIOScheduler instance.

        Raises:
            RuntimeError: If scheduler not initialized.
        """
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized")
        return self._scheduler

    async def start(self) -> None:
        """Start the scheduler.

        This method should be called during application startup.
        It starts the scheduler in a non-blocking manner.
        """
        if self._started:
            logger.warning("Scheduler already started")
            return

        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized")

        self._scheduler.start(paused=False)
        self._started = True

        # Log existing jobs on startup
        jobs = self._scheduler.get_jobs()
        logger.info(f"Scheduler started with {len(jobs)} existing jobs")

    async def shutdown(self, wait: bool = True) -> None:
        """Shutdown the scheduler gracefully.

        Args:
            wait: If True, wait for running jobs to complete.
                 If False, terminate immediately.
        """
        if not self._started:
            logger.warning("Scheduler not running, nothing to shutdown")
            return

        if self._scheduler is None:
            logger.warning("Scheduler not initialized")
            return

        self._scheduler.shutdown(wait=wait)
        self._started = False
        logger.info("Scheduler shutdown complete")

    def is_running(self) -> bool:
        """Check if the scheduler is currently running.

        Returns:
            True if scheduler is running, False otherwise.
        """
        return self._started and self._scheduler is not None and self._scheduler.running

    async def get_status(self) -> dict[str, Any]:
        """Get scheduler status for health checks.

        Returns:
            Dictionary containing scheduler status information.
        """
        if not self._scheduler:
            return {
                "status": "not_initialized",
            }

        if not self._started:
            return {
                "status": "stopped",
            }

        jobs = self._scheduler.get_jobs()
        pending_jobs = [job for job in jobs if job.next_run_time is not None]

        # Get next run time if any jobs are pending
        next_run: datetime | None = None
        if pending_jobs:
            next_run = min(job.next_run_time for job in pending_jobs if job.next_run_time)

        return {
            "status": "running" if self._scheduler.running else "paused",
            "job_store_type": self.settings.job_store_type,
            "total_jobs": len(jobs),
            "pending_jobs": len(pending_jobs),
            "next_run_time": next_run.isoformat() if next_run else None,
        }

    def get_jobs(self) -> list[Any]:
        """Get all scheduled jobs.

        Returns:
            List of APScheduler Job objects.
        """
        if not self._scheduler:
            return []
        return self._scheduler.get_jobs()

    def get_job(self, job_id: str) -> Any | None:
        """Get a specific job by ID.

        Args:
            job_id: The job identifier.

        Returns:
            The Job object if found, None otherwise.
        """
        if not self._scheduler:
            return None
        return self._scheduler.get_job(job_id)

    def remove_job(self, job_id: str) -> None:
        """Remove a scheduled job.

        Args:
            job_id: The job identifier to remove.
        """
        if self._scheduler:
            self._scheduler.remove_job(job_id)
            logger.info(f"Removed scheduled job: {job_id}")

    @property
    def executor(self) -> "ScheduledJobExecutor | None":
        """Get the job executor for handling scheduled downloads.

        Returns:
            The executor instance if set, None otherwise.
        """
        return self._executor

    def set_executor(self, executor: "ScheduledJobExecutor") -> None:
        """Set the job executor for handling scheduled downloads.

        Args:
            executor: The executor instance to use for job execution.
        """
        self._executor = executor
        logger.info("Executor attached to scheduler service")


def get_scheduler_service(
    redis_uri: str | None,
    settings: SchedulerConfig,
) -> SchedulerService:
    """Factory function to create a SchedulerService instance.

    Args:
        redis_uri: Redis connection URI for job persistence.
        settings: Scheduler configuration settings.

    Returns:
        Configured SchedulerService instance.
    """
    return SchedulerService(redis_uri=redis_uri, settings=settings)

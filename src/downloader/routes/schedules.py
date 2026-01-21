"""Schedule management endpoints for recurring downloads."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Path, Query

from ..auth import get_api_key
from ..dependencies import ExecutionStorageDep, SchedulerDep
from ..models.responses import ErrorResponse
from ..models.schedule import (
    ScheduleCreate,
    ScheduleExecutionListResponse,
    ScheduleListResponse,
    ScheduleResponse,
)
from ..scheduler import SchedulerService

if TYPE_CHECKING:
    from ..scheduler import ScheduledJobExecutor

logger = logging.getLogger(__name__)

# Module-level executor reference (set at startup via set_executor())
_executor: ScheduledJobExecutor | None = None


def set_executor(executor: ScheduledJobExecutor) -> None:
    """Set the executor instance for scheduled jobs.

    This function is called at application startup to inject the executor
    dependency into this module. APScheduler jobs run outside FastAPI's
    request context, so we use this module-level reference instead.

    Args:
        executor: The executor instance to use for job execution.
    """
    global _executor
    _executor = executor
    logger.info("Executor set for scheduled jobs")


router = APIRouter()


def _require_scheduler(scheduler: SchedulerDep) -> SchedulerService:
    """Get scheduler service or raise 503 if not available."""
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Scheduler service is not available. Redis connection required.",
                error_type="service_unavailable",
            ).model_dump(),
        )
    return scheduler


async def scheduled_download_job(
    schedule_id: str,
    url: str,
    format: str,
    headers: dict[str, str] | None,
) -> None:
    """Job function executed by APScheduler for scheduled downloads.

    This function is called by APScheduler when a scheduled job triggers.
    It delegates to the executor for actual download execution with retry logic.

    Args:
        schedule_id: Unique identifier for the schedule.
        url: URL to download.
        format: Output format (text, markdown, html, pdf, json, raw).
        headers: Optional custom HTTP headers.
    """
    if _executor is None:
        logger.error(f"[SCHEDULE-{schedule_id}] Executor not initialized, cannot execute job")
        return

    logger.info(f"[SCHEDULE-{schedule_id}] Starting execution: {url} (format={format})")

    try:
        execution = await _executor.execute(schedule_id, url, format, headers)

        if execution.success:
            logger.info(
                f"[SCHEDULE-{schedule_id}] Completed in {execution.duration:.2f}s "
                f"({execution.content_size or 0} bytes)"
            )
        else:
            logger.error(
                f"[SCHEDULE-{schedule_id}] Failed after {execution.attempt} attempts: "
                f"{execution.error_message}"
            )
    except Exception as e:
        logger.exception(f"[SCHEDULE-{schedule_id}] Unexpected error during execution: {e}")


def _format_cron_trigger(trigger: CronTrigger) -> str:
    """Format CronTrigger back to cron expression string."""
    if hasattr(trigger, "fields"):
        # APScheduler CronTrigger fields: second, minute, hour, day, month, day_of_week, year
        # We extract minute through day_of_week (indices 1-5) for standard 5-field cron
        fields = trigger.fields
        if len(fields) >= 6:
            return f"{fields[1]} {fields[2]} {fields[3]} {fields[4]} {fields[5]}"
    # Fallback: return string representation
    return str(trigger)


@router.post("/schedules", response_model=ScheduleResponse)
async def create_schedule(
    schedule_data: ScheduleCreate,
    scheduler: SchedulerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> ScheduleResponse:
    """Create a new scheduled download job.

    The cron expression must be in standard 5-field UNIX format:
    minute hour day_of_month month day_of_week

    Examples:
    - "0 9 * * *" - Daily at 9 AM
    - "0 9 * * 1-5" - Weekdays at 9 AM
    - "*/15 * * * *" - Every 15 minutes
    """
    svc = _require_scheduler(scheduler)

    try:
        # Generate unique ID
        schedule_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc)

        # Create cron trigger from expression
        trigger = CronTrigger.from_crontab(schedule_data.cron_expression)

        # Store schedule metadata in job's kwargs for later retrieval
        job_kwargs = {
            "schedule_id": schedule_id,
            "url": schedule_data.url,
            "format": schedule_data.format,
            "headers": schedule_data.headers,
            "created_at": created_at.isoformat(),
        }

        # Add job to scheduler
        job = svc.scheduler.add_job(
            func=scheduled_download_job,
            trigger=trigger,
            id=schedule_id,
            name=schedule_data.name,
            kwargs=job_kwargs,
            replace_existing=False,
        )

        # Pause job if not enabled
        if not schedule_data.enabled:
            job.pause()

        logger.info(f"Created schedule {schedule_id}: {schedule_data.name}")

        return ScheduleResponse(
            id=schedule_id,
            name=schedule_data.name,
            url=schedule_data.url,
            cron_expression=schedule_data.cron_expression,
            format=schedule_data.format,
            headers=schedule_data.headers,
            enabled=schedule_data.enabled,
            created_at=created_at,
            next_run_time=job.next_run_time,
        )

    except ValueError as e:
        # Cron validation error (should be caught by Pydantic, but handle here too)
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=str(e),
                error_type="validation_error",
            ).model_dump(),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to create schedule: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to create schedule",
                error_type="schedule_creation_error",
            ).model_dump(),
        ) from e


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    scheduler: SchedulerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> ScheduleListResponse:
    """List all scheduled download jobs."""
    svc = _require_scheduler(scheduler)

    try:
        jobs = svc.get_jobs()

        schedules = []
        for job in jobs:
            # Extract schedule data from job kwargs
            kwargs = job.kwargs or {}

            # Parse created_at from kwargs, fallback to now if not stored
            created_at_str = kwargs.get("created_at")
            if created_at_str:
                created_at = datetime.fromisoformat(created_at_str)
            else:
                created_at = datetime.now(timezone.utc)

            schedules.append(
                ScheduleResponse(
                    id=job.id,
                    name=job.name or "",
                    url=kwargs.get("url", ""),
                    cron_expression=_format_cron_trigger(job.trigger),
                    format=kwargs.get("format", "text"),
                    headers=kwargs.get("headers"),
                    enabled=job.next_run_time is not None,
                    created_at=created_at,
                    next_run_time=job.next_run_time,
                )
            )

        return ScheduleListResponse(
            schedules=schedules,
            total=len(schedules),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to list schedules: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to list schedules",
                error_type="schedule_list_error",
            ).model_dump(),
        ) from e


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> ScheduleResponse:
    """Get details of a specific scheduled job."""
    svc = _require_scheduler(scheduler)

    try:
        job = svc.get_job(schedule_id)

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Schedule {schedule_id} not found",
                    error_type="schedule_not_found",
                ).model_dump(),
            )

        kwargs = job.kwargs or {}

        # Parse created_at from kwargs
        created_at_str = kwargs.get("created_at")
        if created_at_str:
            created_at = datetime.fromisoformat(created_at_str)
        else:
            created_at = datetime.now(timezone.utc)

        return ScheduleResponse(
            id=job.id,
            name=job.name or "",
            url=kwargs.get("url", ""),
            cron_expression=_format_cron_trigger(job.trigger),
            format=kwargs.get("format", "text"),
            headers=kwargs.get("headers"),
            enabled=job.next_run_time is not None,
            created_at=created_at,
            next_run_time=job.next_run_time,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get schedule {schedule_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to get schedule",
                error_type="schedule_get_error",
            ).model_dump(),
        ) from e


@router.get("/schedules/{schedule_id}/history", response_model=ScheduleExecutionListResponse)
async def get_schedule_history(
    schedule_id: str = Path(..., description="Schedule identifier"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results per page"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    scheduler: SchedulerDep = None,
    execution_storage: ExecutionStorageDep = None,
    api_key: str | None = Depends(get_api_key),
) -> ScheduleExecutionListResponse:
    """Get execution history for a scheduled job.

    Returns past executions ordered by start time (newest first).
    Each execution includes start time, duration, status, and error message if failed.
    """
    svc = _require_scheduler(scheduler)

    try:
        # Verify schedule exists
        job = svc.get_job(schedule_id)

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Schedule {schedule_id} not found",
                    error_type="schedule_not_found",
                ).model_dump(),
            )

        # Check execution storage is available
        if execution_storage is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorResponse(
                    error="Execution storage is not available. Redis connection required.",
                    error_type="service_unavailable",
                ).model_dump(),
            )

        # Get executions with pagination
        executions = await execution_storage.get_executions(schedule_id, limit, offset)
        total = await execution_storage.get_execution_count(schedule_id)

        return ScheduleExecutionListResponse(
            executions=executions,
            total=total,
            limit=limit,
            offset=offset,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to get history for schedule {schedule_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to get schedule history",
                error_type="schedule_history_error",
            ).model_dump(),
        ) from e


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> dict:
    """Remove a scheduled job."""
    svc = _require_scheduler(scheduler)

    try:
        job = svc.get_job(schedule_id)

        if job is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Schedule {schedule_id} not found",
                    error_type="schedule_not_found",
                ).model_dump(),
            )

        svc.remove_job(schedule_id)
        logger.info(f"Deleted schedule {schedule_id}")

        return {
            "success": True,
            "message": f"Schedule {schedule_id} deleted successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to delete schedule {schedule_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to delete schedule",
                error_type="schedule_deletion_error",
            ).model_dump(),
        ) from e

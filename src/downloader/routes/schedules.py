"""Schedule management endpoints for recurring downloads."""

import logging
import uuid
from datetime import datetime, timezone

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Path

from ..auth import get_api_key
from ..dependencies import SchedulerDep
from ..models.responses import ErrorResponse
from ..models.schedule import ScheduleCreate, ScheduleListResponse, ScheduleResponse
from ..scheduler import SchedulerService

logger = logging.getLogger(__name__)

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

    This is a placeholder - full implementation in S3-BE-3 (executor).
    For now, just log the execution.
    """
    logger.info(f"[SCHEDULE-{schedule_id}] Executing download: {url} (format={format})")
    # TODO: S3-BE-3 will implement actual download execution


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

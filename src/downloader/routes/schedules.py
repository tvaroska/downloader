"""Schedule management endpoints for recurring downloads."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request

from ..auth import get_api_key
from ..config import Settings, get_settings
from ..dependencies import ExecutionStorageDep, SchedulerDep
from ..models.responses import ErrorResponse
from ..models.schedule import (
    ScheduleCreate,
    ScheduleExecutionListResponse,
    ScheduleListResponse,
    ScheduleResponse,
)
from ..scheduler import SchedulerService
from ..validation import URLValidationError, sanitize_custom_headers, validate_url_async

if TYPE_CHECKING:
    from ..scheduler import ScheduledJobExecutor

logger = logging.getLogger(__name__)

_executor: ScheduledJobExecutor | None = None

router = APIRouter()


def set_executor(executor: ScheduledJobExecutor | None) -> None:
    """Register the `ScheduledJobExecutor` invoked by APScheduler cron triggers."""
    global _executor
    _executor = executor
    if executor is not None:
        logger.info("Executor set for scheduled jobs")


def _require_scheduler(scheduler: SchedulerDep) -> SchedulerService:
    if scheduler is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Scheduling is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )
    return scheduler


async def scheduled_download_job(  # noqa: PLR0913
    schedule_id: str,
    url: str,
    format: str,
    headers: dict[str, str] | None,
    webhook_url: str | None = None,
    webhook_secret: str | None = None,
    **_extra: Any,
) -> None:
    """Entrypoint invoked by APScheduler when a scheduled cron job fires."""
    if _executor is None:
        logger.error(f"[SCHEDULE-{schedule_id}] Executor not initialized, cannot execute job")
        return

    logger.info(f"[SCHEDULE-{schedule_id}] Starting execution: {url} (format={format})")
    try:
        execution = await _executor.execute(
            schedule_id,
            url,
            format,
            headers,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
        )
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
    except Exception as exc:  # noqa: BLE001
        # Prevent unhandled callback exceptions from crashing APScheduler's event-loop runner.
        logger.exception(f"[SCHEDULE-{schedule_id}] Unexpected error during execution: {exc}")


def _format_cron_trigger(trigger: CronTrigger) -> str:
    if hasattr(trigger, "fields"):
        fields = trigger.fields
        if len(fields) >= 6:
            return f"{fields[1]} {fields[2]} {fields[3]} {fields[4]} {fields[5]}"
    return str(trigger)


def _job_to_schedule_response(job: Any) -> ScheduleResponse:
    kwargs = job.kwargs or {}
    created_at_str = kwargs.get("created_at")
    created_at = datetime.fromisoformat(created_at_str) if created_at_str else datetime.now(UTC)
    stored_expr = kwargs.get("cron_expression")
    cron_expr = stored_expr if isinstance(stored_expr, str) else _format_cron_trigger(job.trigger)
    return ScheduleResponse(
        id=job.id,
        name=job.name or "",
        url=kwargs.get("url", ""),
        cron_expression=cron_expr,
        format=kwargs.get("format", "text"),
        headers=kwargs.get("headers"),
        enabled=job.next_run_time is not None,
        created_at=created_at,
        next_run_time=job.next_run_time,
        webhook_url=kwargs.get("webhook_url"),
    )


@router.post("/schedules", response_model=ScheduleResponse)
async def create_schedule(
    schedule_data: ScheduleCreate,
    request: Request,
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> ScheduleResponse:
    """Create a new scheduled download job."""
    svc = _require_scheduler(scheduler)
    active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()

    try:
        validated_url = await validate_url_async(schedule_data.url, active_settings)
        validated_webhook_url = (
            await validate_url_async(schedule_data.webhook_url, active_settings)
            if schedule_data.webhook_url
            else None
        )
        sanitized_headers = sanitize_custom_headers(schedule_data.headers)
        trigger = CronTrigger.from_crontab(schedule_data.cron_expression)
    except (ValueError, URLValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(exc), error_type="validation_error").model_dump(),
        ) from exc

    try:
        schedule_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        job_kwargs = {
            "schedule_id": schedule_id,
            "url": validated_url,
            "format": schedule_data.format,
            "headers": sanitized_headers,
            "created_at": created_at.isoformat(),
            "cron_expression": schedule_data.cron_expression,
            "webhook_url": validated_webhook_url,
            "webhook_secret": schedule_data.webhook_secret,
        }
        job = svc.scheduler.add_job(
            func=scheduled_download_job,
            trigger=trigger,
            id=schedule_id,
            name=schedule_data.name,
            kwargs=job_kwargs,
            replace_existing=False,
        )
        if not schedule_data.enabled:
            job.pause()

        logger.info(f"Created schedule {schedule_id}: {schedule_data.name}")
        return ScheduleResponse(
            id=schedule_id,
            name=schedule_data.name,
            url=validated_url,
            cron_expression=schedule_data.cron_expression,
            format=schedule_data.format,
            headers=sanitized_headers,
            enabled=schedule_data.enabled,
            created_at=created_at,
            next_run_time=job.next_run_time,
            webhook_url=validated_webhook_url,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to create schedule: {exc}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to create schedule",
                error_type="schedule_creation_error",
            ).model_dump(),
        ) from exc


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> ScheduleListResponse:
    """List all scheduled download jobs."""
    svc = _require_scheduler(scheduler)
    try:
        schedules = [_job_to_schedule_response(job) for job in svc.get_jobs()]
        return ScheduleListResponse(schedules=schedules, total=len(schedules))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to list schedules: {exc}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to list schedules",
                error_type="schedule_list_error",
            ).model_dump(),
        ) from exc


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
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
        return _job_to_schedule_response(job)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to get schedule {schedule_id}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to get schedule",
                error_type="schedule_get_error",
            ).model_dump(),
        ) from exc


@router.get("/schedules/{schedule_id}/history", response_model=ScheduleExecutionListResponse)
async def get_schedule_history(  # noqa: PLR0913
    schedule_id: str = Path(..., description="Schedule identifier"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results per page"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
    scheduler: SchedulerDep = None,
    execution_storage: ExecutionStorageDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> ScheduleExecutionListResponse:
    """Get paginated execution history for a scheduled job."""
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
        if execution_storage is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorResponse(
                    error="Schedule execution history is not available. Redis connection (REDIS_URI) is required.",
                    error_type="service_unavailable",
                ).model_dump(),
            )

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
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to get history for schedule {schedule_id}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to get schedule history",
                error_type="schedule_history_error",
            ).model_dump(),
        ) from exc


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
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
        return {"success": True, "message": f"Schedule {schedule_id} deleted successfully"}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to delete schedule {schedule_id}: {exc}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to delete schedule",
                error_type="schedule_deletion_error",
            ).model_dump(),
        ) from exc

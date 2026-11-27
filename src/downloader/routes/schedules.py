"""Schedule management endpoints for recurring downloads."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NoReturn

import redis.exceptions as redis_exc
from apscheduler.jobstores.base import JobLookupError
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
from ..scheduler.cron import cron_trigger_from_unix_crontab
from ..services.webhooks import seal_webhook_secret
from ..validation import (
    URLValidationError,
    redact_url_for_log,
    sanitize_custom_headers,
    validate_url_async,
)

if TYPE_CHECKING:
    from ..scheduler import ScheduledJobExecutor

logger = logging.getLogger(__name__)

_executor: ScheduledJobExecutor | None = None

_RELEASE_RUN_LOCK_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_CRON_FIELD_ORDER = ("minute", "hour", "day", "month", "day_of_week")

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


def _raise_schedule_store_error(
    exc: Exception,
    message: str,
    error_type: str,
    *,
    schedule_id: str | None = None,
) -> NoReturn:
    if isinstance(exc, JobLookupError):
        detail_msg = f"Schedule {schedule_id} not found" if schedule_id else "Schedule not found"
        raise HTTPException(
            status_code=404,
            detail=ErrorResponse(error=detail_msg, error_type="schedule_not_found").model_dump(),
        ) from exc
    if isinstance(exc, (redis_exc.RedisError, OSError, TimeoutError)):
        logger.warning(f"{message} (storage unavailable): {exc}")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Schedule storage backend temporarily unavailable",
                error_type="service_unavailable",
            ).model_dump(),
        ) from exc
    logger.exception(f"{message}: {exc}")
    raise HTTPException(
        status_code=500,
        detail=ErrorResponse(error=message, error_type=error_type).model_dump(),
    ) from exc


@asynccontextmanager
async def _schedule_run_lock(schedule_id: str, ttl_seconds: int = 50) -> AsyncIterator[bool]:
    """Acquire a token-owned per-run Redis lock and release it cleanly on completion."""
    if _executor is None:
        yield True
        return
    storage = getattr(_executor, "storage", None)
    redis_client = getattr(storage, "redis", None)
    if redis_client is None:
        yield True
        return

    lock_key = f"schedule:run_lock:{schedule_id}"
    token = uuid.uuid4().hex
    acquired = False
    try:
        acquired = bool(await redis_client.set(lock_key, token, nx=True, ex=ttl_seconds))
    except (redis_exc.RedisError, OSError, RuntimeError) as exc:
        logger.warning(f"[SCHEDULE-{schedule_id}] Failed to acquire run lock: {exc}")
        yield True
        return

    try:
        yield acquired
    finally:
        if acquired:
            try:
                await redis_client.eval(_RELEASE_RUN_LOCK_LUA, 1, lock_key, token)
            except (redis_exc.RedisError, OSError, RuntimeError, AttributeError) as exc:
                logger.debug(f"[SCHEDULE-{schedule_id}] Run lock release skipped: {exc}")


async def scheduled_download_job(  # noqa: PLR0913
    schedule_id: str,
    url: str,
    format: str,
    headers: dict[str, str] | None,
    webhook_url: str | None = None,
    webhook_secret_enc: str | None = None,
    webhook_secret: str | None = None,
    **_extra: Any,
) -> None:
    """Entrypoint invoked by APScheduler when a scheduled cron job fires."""
    if _executor is None:
        logger.error(f"[SCHEDULE-{schedule_id}] Executor not initialized, cannot execute job")
        return

    async with _schedule_run_lock(schedule_id) as acquired:
        if not acquired:
            logger.info(
                f"[SCHEDULE-{schedule_id}] Skipping duplicate run (lock held by another worker)"
            )
            return

        logger.info(
            f"[SCHEDULE-{schedule_id}] Starting execution: {redact_url_for_log(url)} (format={format})"
        )
        try:
            execution = await _executor.execute(
                schedule_id,
                url,
                format,
                headers,
                webhook_url=webhook_url,
                webhook_secret=webhook_secret_enc or webhook_secret,
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
        except (redis_exc.RedisError, OSError, RuntimeError, ValueError, TypeError) as exc:
            logger.exception(f"[SCHEDULE-{schedule_id}] Unexpected error during execution: {exc}")


def _extract_cron_fields(trigger: Any) -> dict[str, str] | None:
    fields = getattr(trigger, "fields", None)
    if not fields:
        return None
    by_name = {getattr(field, "name", ""): str(field) for field in fields}
    if all(name in by_name for name in _CRON_FIELD_ORDER):
        return by_name
    return None


def _reconstruct_legacy_cron_expr(trigger: Any) -> str:
    """Reconstruct a 5-field cron string from a pre-`c68ee45` APScheduler `CronTrigger` or `OrTrigger`."""
    direct = _extract_cron_fields(trigger)
    if direct is not None:
        return " ".join(direct[name] for name in _CRON_FIELD_ORDER)

    sub_triggers = getattr(trigger, "triggers", None)
    if isinstance(sub_triggers, (list, tuple)) and len(sub_triggers) == 2:
        first = _extract_cron_fields(sub_triggers[0])
        second = _extract_cron_fields(sub_triggers[1])
        if (
            first is not None
            and second is not None
            and all(first[k] == second[k] for k in ("minute", "hour", "month"))
        ):
            day = first["day"] if first["day"] != "*" else second["day"]
            dow = first["day_of_week"] if first["day_of_week"] != "*" else second["day_of_week"]
            return f"{first['minute']} {first['hour']} {day} {first['month']} {dow}"

    return str(trigger)


def _job_to_schedule_response(job: Any) -> ScheduleResponse:
    kwargs = job.kwargs or {}
    created_at_str = kwargs.get("created_at")
    created_at = datetime.fromisoformat(created_at_str) if created_at_str else datetime.now(UTC)
    stored_expr = kwargs.get("cron_expression")
    cron_expr = (
        stored_expr if isinstance(stored_expr, str) else _reconstruct_legacy_cron_expr(job.trigger)
    )
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


def _add_schedule_job_sync(
    svc: SchedulerService,
    *,
    trigger: Any,
    schedule_id: str,
    schedule_data: ScheduleCreate,
    job_kwargs: dict[str, Any],
) -> Any:
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
    svc.invalidate_status_cache()
    return job


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
        trigger = cron_trigger_from_unix_crontab(schedule_data.cron_expression)
    except (ValueError, URLValidationError) as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(exc), error_type="validation_error").model_dump(),
        ) from exc

    try:
        sealed_secret = seal_webhook_secret(schedule_data.webhook_secret, active_settings)
        schedule_id = str(uuid.uuid4())
        created_at = datetime.now(UTC)
        job_kwargs: dict[str, Any] = {
            "schedule_id": schedule_id,
            "url": validated_url,
            "format": schedule_data.format,
            "headers": sanitized_headers,
            "created_at": created_at.isoformat(),
            "cron_expression": schedule_data.cron_expression,
            "webhook_url": validated_webhook_url,
            "webhook_secret_enc": sealed_secret,
        }
        job = await asyncio.to_thread(
            _add_schedule_job_sync,
            svc,
            trigger=trigger,
            schedule_id=schedule_id,
            schedule_data=schedule_data,
            job_kwargs=job_kwargs,
        )

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
    except (
        JobLookupError,
        redis_exc.RedisError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        _raise_schedule_store_error(exc, "Failed to create schedule", "schedule_creation_error")


@router.get("/schedules", response_model=ScheduleListResponse)
async def list_schedules(
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> ScheduleListResponse:
    """List all scheduled download jobs."""
    svc = _require_scheduler(scheduler)
    try:
        jobs = await asyncio.to_thread(svc.get_jobs)
        schedules = [_job_to_schedule_response(job) for job in jobs]
        return ScheduleListResponse(schedules=schedules, total=len(schedules))
    except HTTPException:
        raise
    except (
        redis_exc.RedisError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        _raise_schedule_store_error(exc, "Failed to list schedules", "schedule_list_error")


@router.get("/schedules/{schedule_id}", response_model=ScheduleResponse)
async def get_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> ScheduleResponse:
    """Get details of a specific scheduled job."""
    svc = _require_scheduler(scheduler)
    try:
        job = await asyncio.to_thread(svc.get_job, schedule_id)
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
    except (
        JobLookupError,
        redis_exc.RedisError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        _raise_schedule_store_error(
            exc,
            "Failed to get schedule",
            "schedule_get_error",
            schedule_id=schedule_id,
        )


@router.get("/schedules/{schedule_id}/history", response_model=ScheduleExecutionListResponse)
async def get_schedule_history(
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
        job = await asyncio.to_thread(svc.get_job, schedule_id)
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
    except (
        JobLookupError,
        redis_exc.RedisError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        _raise_schedule_store_error(
            exc,
            "Failed to get schedule history",
            "schedule_history_error",
            schedule_id=schedule_id,
        )


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str = Path(..., description="Schedule identifier"),
    scheduler: SchedulerDep = None,
    execution_storage: ExecutionStorageDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Remove a scheduled job and its execution history."""
    svc = _require_scheduler(scheduler)
    try:
        job = await asyncio.to_thread(svc.get_job, schedule_id)
        if job is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Schedule {schedule_id} not found",
                    error_type="schedule_not_found",
                ).model_dump(),
            )
        await asyncio.to_thread(svc.remove_job, schedule_id)
        if execution_storage is not None:
            await execution_storage.delete_executions(schedule_id)
        logger.info(f"Deleted schedule {schedule_id}")
        return {"success": True, "message": f"Schedule {schedule_id} deleted successfully"}
    except HTTPException:
        raise
    except (
        JobLookupError,
        redis_exc.RedisError,
        OSError,
        TimeoutError,
        RuntimeError,
        ValueError,
        TypeError,
    ) as exc:
        _raise_schedule_store_error(
            exc,
            "Failed to delete schedule",
            "schedule_deletion_error",
            schedule_id=schedule_id,
        )

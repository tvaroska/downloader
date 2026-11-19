"""Batch processing endpoints backed by `JobStore` and `WorkQueue` ports."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import redis.exceptions as redis_exc
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from ..auth import get_api_key
from ..config import Settings, get_settings
from ..dependencies import (
    BatchSemaphoreDep,
    BrowserRendererDep,
    DomainThrottleDep,
    HTTPClientDep,
    JobStoreDep,
    PDFSemaphoreDep,
    RobotsCheckerDep,
    WorkQueueDep,
)
from ..models.responses import (
    BatchRequest,
    ErrorResponse,
    HTTPErrorResponse,
    JobStatusResponse,
    JobSubmissionResponse,
)
from ..ports import JobInfo, JobStatus, JobStore
from ..services.batch_processor import process_background_batch_job
from ..services.webhooks import seal_webhook_secret
from ..validation import URLValidationError, sanitize_custom_headers, validate_url_async

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router"]


def _require_job_store(job_store: JobStore | None) -> JobStore:
    if job_store is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )
    return job_store


def _unavailable_results_message(job_id: str, job_info: JobInfo) -> str:
    if job_info.status == JobStatus.PENDING:
        return f"Job {job_id} is still pending"
    if job_info.status == JobStatus.RUNNING:
        return f"Job {job_id} is still running ({job_info.progress}% complete)"
    if job_info.status == JobStatus.FAILED:
        return f"Job {job_id} failed: {job_info.error_message}"
    if job_info.status == JobStatus.CANCELLED:
        return f"Job {job_id} was cancelled"
    return f"Results not available for job {job_id}"


def _raise_batch_store_error(exc: Exception, message: str, error_type: str) -> NoReturn:
    if isinstance(exc, (redis_exc.RedisError, OSError, TimeoutError)):
        logger.warning(f"{message} (storage unavailable): {exc}")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch storage backend temporarily unavailable",
                error_type="service_unavailable",
            ).model_dump(),
        ) from exc
    logger.exception(f"{message}: {exc}")
    raise HTTPException(
        status_code=500,
        detail=ErrorResponse(error=message, error_type=error_type).model_dump(),
    ) from exc


async def _prepare_persisted_batch_payload(
    batch_request: BatchRequest,
    active_settings: Settings,
) -> dict[str, Any]:
    try:
        for item in batch_request.urls:
            item.custom_headers = sanitize_custom_headers(item.custom_headers)
        if batch_request.webhook_url:
            batch_request.webhook_url = await validate_url_async(
                batch_request.webhook_url, active_settings
            )
    except URLValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(exc), error_type="validation_error").model_dump(),
        ) from exc

    # Never persist cleartext signing secrets in Redis; seal only when split workers need them.
    persisted_payload = batch_request.model_dump(exclude={"webhook_secret"})
    if active_settings.worker.deployment_mode == "api" and batch_request.webhook_secret:
        try:
            persisted_payload["webhook_secret_enc"] = seal_webhook_secret(
                batch_request.webhook_secret,
                active_settings,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=500,
                detail=ErrorResponse(
                    error=str(exc),
                    error_type="job_submission_error",
                ).model_dump(),
            ) from exc
    return persisted_payload


@router.post(
    "/batch",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid batch request"},
        500: {"model": HTTPErrorResponse, "description": "Job submission error"},
        503: {"model": HTTPErrorResponse, "description": "Redis unavailable"},
    },
)
async def submit_batch_job(  # noqa: PLR0913
    batch_request: BatchRequest,
    request: Request,
    http_client: HTTPClientDep,
    batch_semaphore: BatchSemaphoreDep,
    pdf_semaphore: PDFSemaphoreDep,
    renderer: BrowserRendererDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    job_manager: JobStoreDep = None,
    worker_queue: WorkQueueDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> JobSubmissionResponse:
    """Submit a batch processing job for background or split-worker execution."""
    store = _require_job_store(job_manager)
    active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()

    max_urls = active_settings.batch.max_urls_per_batch
    if len(batch_request.urls) > max_urls:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=f"Too many URLs in batch request. Maximum is {max_urls} URLs.",
                error_type="validation_error",
            ).model_dump(),
        )

    persisted_payload = await _prepare_persisted_batch_payload(batch_request, active_settings)

    try:
        job_id = await store.create_job(persisted_payload)
        if active_settings.worker.deployment_mode == "api":
            if worker_queue is None:
                raise RuntimeError("WorkQueue unavailable in API split-worker mode")
            await worker_queue.enqueue_batch_job(job_id)
        else:
            await store.start_background_job(
                job_id,
                process_background_batch_job,
                batch_request,
                store,
                batch_semaphore,
                http_client,
                pdf_semaphore,
                robots_checker,
                domain_throttle,
                renderer=renderer,
                settings=active_settings,
            )

        logger.info(f"[JOB-{job_id}] Submitted batch job with {len(batch_request.urls)} URLs")
        estimated_seconds = len(batch_request.urls) * 2
        estimated_completion = (
            (datetime.now(UTC) + timedelta(seconds=estimated_seconds)).isoformat()
            if estimated_seconds < 300
            else None
        )
        return JobSubmissionResponse(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
            total_urls=len(batch_request.urls),
            estimated_completion=estimated_completion,
        )
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
        _raise_batch_store_error(exc, "Failed to submit batch job", "job_submission_error")


@router.get(
    "/jobs/{job_id}/status",
    responses={
        404: {"model": HTTPErrorResponse, "description": "Job not found"},
        500: {"model": HTTPErrorResponse, "description": "Status retrieval error"},
        503: {"model": HTTPErrorResponse, "description": "Redis unavailable"},
    },
)
async def get_job_status(
    job_id: str = Path(..., description="Job identifier"),
    job_manager: JobStoreDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> JobStatusResponse:
    """Get the status of a batch processing job."""
    store = _require_job_store(job_manager)
    try:
        job_info = await store.get_job_info(job_id)
        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )
        return JobStatusResponse(
            job_id=job_info.job_id,
            status=job_info.status.value,
            progress=job_info.progress,
            created_at=job_info.created_at.isoformat(),
            started_at=job_info.started_at.isoformat() if job_info.started_at else None,
            completed_at=job_info.completed_at.isoformat() if job_info.completed_at else None,
            total_urls=job_info.total_urls,
            processed_urls=job_info.processed_urls,
            successful_urls=job_info.successful_urls,
            failed_urls=job_info.failed_urls,
            error_message=job_info.error_message,
            results_available=job_info.results_available,
            expires_at=job_info.expires_at.isoformat() if job_info.expires_at else None,
        )
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
        _raise_batch_store_error(exc, "Failed to retrieve job status", "job_status_error")


@router.get(
    "/jobs/{job_id}/results",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Results not available"},
        404: {"model": HTTPErrorResponse, "description": "Job or results not found"},
        500: {"model": HTTPErrorResponse, "description": "Results retrieval error"},
        503: {"model": HTTPErrorResponse, "description": "Redis unavailable"},
    },
)
async def get_job_results(
    job_id: str = Path(..., description="Job identifier"),
    job_manager: JobStoreDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> Response:
    """Download the results of a completed batch processing job."""
    store = _require_job_store(job_manager)
    try:
        job_info = await store.get_job_info(job_id)
        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        if not job_info.results_available:
            raise HTTPException(
                status_code=400,
                detail=ErrorResponse(
                    error=_unavailable_results_message(job_id, job_info),
                    error_type="results_not_available",
                ).model_dump(),
            )

        job_results = await store.get_job_results(job_id)
        if not job_results:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Results not found for job {job_id}",
                    error_type="results_not_found",
                ).model_dump(),
            )

        return Response(
            content=job_results.model_dump_json(),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="batch_results_{job_id}.json"',
                "X-Job-ID": job_id,
                "X-Job-Status": job_results.status.value,
                "X-Total-Duration": str(job_results.total_duration),
            },
        )
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
        _raise_batch_store_error(exc, "Failed to retrieve job results", "job_results_error")


@router.delete(
    "/jobs/{job_id}",
    responses={
        404: {"model": HTTPErrorResponse, "description": "Job not found"},
        500: {"model": HTTPErrorResponse, "description": "Cancellation error"},
        503: {"model": HTTPErrorResponse, "description": "Redis unavailable"},
    },
)
async def cancel_job(
    job_id: str = Path(..., description="Job identifier"),
    job_manager: JobStoreDep = None,
    _api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Cancel a running batch processing job."""
    store = _require_job_store(job_manager)
    try:
        job_info = await store.get_job_info(job_id)
        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        cancelled = await store.cancel_job(job_id)
        if cancelled:
            return {"success": True, "message": f"Job {job_id} cancelled successfully"}
        return {
            "success": False,
            "message": f"Job {job_id} could not be cancelled (may already be completed)",
        }
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
        _raise_batch_store_error(exc, "Failed to cancel job", "job_cancellation_error")

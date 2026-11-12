"""Batch processing endpoints for background job management."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from ..auth import get_api_key
from ..config import get_settings
from ..dependencies import (
    BatchSemaphoreDep,
    DomainThrottleDep,
    HTTPClientDep,
    JobManagerDep,
    PDFSemaphoreDep,
    RobotsCheckerDep,
)
from ..job_manager import JobStatus
from ..models.responses import (
    BatchRequest,
    ErrorResponse,
    HTTPErrorResponse,
    JobStatusResponse,
    JobSubmissionResponse,
)
from ..pdf_generator import generate_pdf_from_url
from ..services.batch_processor import (
    process_background_batch_job,
    process_single_url_in_batch,
)
from ..services.content_processor import _playwright_fallback_for_content
from ..validation import URLValidationError, sanitize_custom_headers, validate_url_async
from ..worker_queue import WorkerQueue

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = [
    "_playwright_fallback_for_content",
    "generate_pdf_from_url",
    "process_background_batch_job",
    "process_single_url_in_batch",
    "router",
    "validate_url_async",
]


@router.post(
    "/batch",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid batch request"},
        500: {"model": HTTPErrorResponse, "description": "Job submission error"},
        503: {"model": HTTPErrorResponse, "description": "Redis unavailable"},
    },
)
async def submit_batch_job(
    batch_request: BatchRequest,
    request: Request,
    http_client: HTTPClientDep,
    batch_semaphore: BatchSemaphoreDep,
    pdf_semaphore: PDFSemaphoreDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    job_manager: JobManagerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> JobSubmissionResponse:
    """Submit a batch processing job for background or split-worker execution."""
    if job_manager is None:
        logger.warning("Batch processing requested but Redis is not configured")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    active_settings = getattr(request.app.state, "settings", None) or get_settings()
    max_urls = active_settings.batch.max_urls_per_batch
    if len(batch_request.urls) > max_urls:
        logger.warning(f"Too many URLs requested: {len(batch_request.urls)}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=f"Too many URLs in batch request. Maximum is {max_urls} URLs.",
                error_type="validation_error",
            ).model_dump(),
        )

    try:
        for item in batch_request.urls:
            item.custom_headers = sanitize_custom_headers(item.custom_headers)
        if batch_request.webhook_url:
            batch_request.webhook_url = await validate_url_async(
                batch_request.webhook_url, active_settings
            )
    except URLValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=str(e),
                error_type="validation_error",
            ).model_dump(),
        ) from e

    try:
        request_data = batch_request.model_dump()
        job_id = await job_manager.create_job(request_data)

        if active_settings.worker.deployment_mode == "api":
            if job_manager.redis_client is None:
                raise RuntimeError("Redis client unavailable in API split-worker mode")
            queue = WorkerQueue(job_manager.redis_client)
            await queue.enqueue_batch_job(job_id)
        else:
            await job_manager.start_background_job(
                job_id,
                process_background_batch_job,
                batch_request,
                job_manager,
                batch_semaphore,
                http_client,
                pdf_semaphore,
                robots_checker,
                domain_throttle,
            )

        logger.info(f"[JOB-{job_id}] Submitted batch job with {len(batch_request.urls)} URLs")

        estimated_seconds = len(batch_request.urls) * 2
        estimated_completion = None
        if estimated_seconds < 300:
            estimated_completion = (
                datetime.now(UTC) + timedelta(seconds=estimated_seconds)
            ).isoformat()

        return JobSubmissionResponse(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(UTC).isoformat(),
            total_urls=len(batch_request.urls),
            estimated_completion=estimated_completion,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to submit batch job: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to submit batch job",
                error_type="job_submission_error",
            ).model_dump(),
        )


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
    job_manager: JobManagerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> JobStatusResponse:
    """Get the status of a batch processing job."""
    if job_manager is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    try:
        job_info = await job_manager.get_job_info(job_id)

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
    except Exception as e:
        logger.exception(f"Failed to get job status for {job_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to retrieve job status",
                error_type="job_status_error",
            ).model_dump(),
        )


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
    job_manager: JobManagerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """Download the results of a completed batch processing job."""
    if job_manager is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    try:
        job_info = await job_manager.get_job_info(job_id)

        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        if not job_info.results_available:
            if job_info.status == JobStatus.PENDING:
                error_msg = f"Job {job_id} is still pending"
            elif job_info.status == JobStatus.RUNNING:
                error_msg = f"Job {job_id} is still running ({job_info.progress}% complete)"
            elif job_info.status == JobStatus.FAILED:
                error_msg = f"Job {job_id} failed: {job_info.error_message}"
            elif job_info.status == JobStatus.CANCELLED:
                error_msg = f"Job {job_id} was cancelled"
            else:
                error_msg = f"Results not available for job {job_id}"

            raise HTTPException(
                status_code=400,
                detail=ErrorResponse(
                    error=error_msg,
                    error_type="results_not_available",
                ).model_dump(),
            )

        job_results = await job_manager.get_job_results(job_id)
        if not job_results:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Results not found for job {job_id}",
                    error_type="results_not_found",
                ).model_dump(),
            )

        logger.info(f"Downloaded results for job {job_id}")

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
    except Exception as e:
        logger.exception(f"Failed to get job results for {job_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to retrieve job results",
                error_type="job_results_error",
            ).model_dump(),
        )


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
    job_manager: JobManagerDep = None,
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """Cancel a running batch processing job."""
    if job_manager is None:
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    try:
        job_info = await job_manager.get_job_info(job_id)

        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        cancelled = await job_manager.cancel_job(job_id)

        if cancelled:
            logger.info(f"Successfully cancelled job {job_id}")
            return {
                "success": True,
                "message": f"Job {job_id} cancelled successfully",
            }
        else:
            return {
                "success": False,
                "message": f"Job {job_id} could not be cancelled (may already be completed)",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to cancel job {job_id}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to cancel job",
                error_type="job_cancellation_error",
            ).model_dump(),
        )

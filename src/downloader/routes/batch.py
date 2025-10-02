"""Batch processing endpoints for background job management."""

import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response

from ..auth import get_api_key
from ..content_converter import (
    convert_content_to_markdown,
    convert_content_to_text,
)
from ..dependencies import HTTPClientDep, JobManagerDep, PDFSemaphoreDep, BatchSemaphoreDep
from ..http_client import (
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
)
from ..job_manager import JobStatus
from ..models.responses import (
    BatchRequest,
    BatchURLRequest,
    BatchURLResult,
    ErrorResponse,
    JobStatusResponse,
    JobSubmissionResponse,
)
from ..pdf_generator import PDFGeneratorError, generate_pdf_from_url
from ..services.content_processor import _playwright_fallback_for_content
from ..validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter()


async def process_single_url_in_batch(
    url_request: BatchURLRequest,
    default_format: str,
    timeout: int,
    request_id: str,
    http_client,
    pdf_semaphore
) -> BatchURLResult:
    """
    Process a single URL within a batch request.

    Args:
        url_request: URL request configuration
        default_format: Default format if not specified
        timeout: Timeout for this request
        request_id: Unique identifier for logging

    Returns:
        BatchURLResult with processing outcome
    """
    start_time = asyncio.get_event_loop().time()
    format_to_use = url_request.format or default_format

    try:
        validated_url = validate_url(url_request.url)
        logger.info(
            f"[{request_id}] Processing batch URL: {validated_url} (format: {format_to_use})"
        )

        content, metadata = await asyncio.wait_for(
            http_client.download(validated_url, RequestPriority.LOW), timeout=timeout
        )

        if format_to_use == "text":
            processed_content = convert_content_to_text(
                content, metadata["content_type"]
            )
            processed_content = await _playwright_fallback_for_content(
                validated_url, processed_content, content, metadata["content_type"], "text", request_id
            )

            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content=processed_content,
                size=len(processed_content.encode("utf-8")),
                content_type=metadata["content_type"],
                duration=duration,
                status_code=200,
            )

        elif format_to_use == "markdown":
            processed_content = convert_content_to_markdown(
                content, metadata["content_type"]
            )
            processed_content = await _playwright_fallback_for_content(
                validated_url, processed_content, content, metadata["content_type"], "markdown", request_id
            )

            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content=processed_content,
                size=len(processed_content.encode("utf-8")),
                content_type=metadata["content_type"],
                duration=duration,
                status_code=200,
            )

        elif format_to_use == "html":
            if "html" in metadata["content_type"].lower():
                processed_content = content.decode("utf-8", errors="ignore")
            else:
                processed_content = content.decode("utf-8", errors="ignore")

            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content=processed_content,
                size=len(content),
                content_type=metadata["content_type"],
                duration=duration,
                status_code=200,
            )

        elif format_to_use == "pdf":
            async with pdf_semaphore:
                pdf_content = await generate_pdf_from_url(validated_url)

            content_b64 = base64.b64encode(pdf_content).decode("utf-8")
            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content_base64=content_b64,
                size=len(pdf_content),
                content_type="application/pdf",
                duration=duration,
                status_code=200,
            )

        elif format_to_use == "json":
            content_b64 = base64.b64encode(content).decode("utf-8")
            json_content = json.dumps(
                {
                    "success": True,
                    "url": metadata["url"],
                    "size": metadata["size"],
                    "content_type": metadata["content_type"],
                    "content": content_b64,
                    "metadata": metadata,
                }
            )

            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content=json_content,
                size=len(json_content),
                content_type="application/json",
                duration=duration,
                status_code=200,
            )

        else:  # raw format
            content_b64 = base64.b64encode(content).decode("utf-8")
            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content_base64=content_b64,
                size=len(content),
                content_type=metadata.get("content_type", "application/octet-stream"),
                duration=duration,
                status_code=200,
            )

    except URLValidationError as e:
        duration = asyncio.get_event_loop().time() - start_time
        logger.warning(f"[{request_id}] URL validation failed: {e}")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error=str(e),
            error_type="validation_error",
            status_code=400,
        )

    except asyncio.TimeoutError:
        duration = asyncio.get_event_loop().time() - start_time
        logger.error(f"[{request_id}] Request timeout after {timeout}s")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error=f"Request timeout after {timeout} seconds",
            error_type="timeout_error",
            status_code=408,
        )

    except HTTPTimeoutError as e:
        duration = asyncio.get_event_loop().time() - start_time
        logger.error(f"[{request_id}] HTTP timeout: {e}")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error=str(e),
            error_type="timeout_error",
            status_code=408,
        )

    except HTTPClientError as e:
        duration = asyncio.get_event_loop().time() - start_time
        logger.error(f"[{request_id}] HTTP client error: {e}")

        status_code = 502
        if "404" in str(e):
            status_code = 404
        elif "403" in str(e):
            status_code = 403
        elif "401" in str(e):
            status_code = 401
        elif "500" in str(e):
            status_code = 502

        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error=str(e),
            error_type="http_error",
            status_code=status_code,
        )

    except PDFGeneratorError as e:
        duration = asyncio.get_event_loop().time() - start_time
        logger.error(f"[{request_id}] PDF generation failed: {e}")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error=f"PDF generation failed: {e}",
            error_type="pdf_generation_error",
            status_code=500,
        )

    except Exception as e:
        duration = asyncio.get_event_loop().time() - start_time
        logger.exception(f"[{request_id}] Unexpected error: {e}")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=duration,
            error="Internal server error",
            error_type="internal_error",
            status_code=500,
        )


async def process_background_batch_job(
    job_id: str, batch_request: BatchRequest, job_manager, batch_semaphore, http_client, pdf_semaphore
) -> tuple[list[dict], dict]:
    """
    Process batch job in background.

    Args:
        job_id: Job identifier for progress tracking
        batch_request: Batch processing configuration
        job_manager: JobManager instance
        batch_semaphore: Batch concurrency semaphore
        http_client: HTTPClient instance
        pdf_semaphore: PDF concurrency semaphore

    Returns:
        Tuple of (results_list, summary_dict)
    """
    start_time = asyncio.get_event_loop().time()

    logger.info(f"[JOB-{job_id}] Starting background batch processing: {len(batch_request.urls)} URLs")

    request_semaphore = asyncio.Semaphore(batch_request.concurrency_limit)

    async def process_with_semaphore(
        url_request: BatchURLRequest, index: int
    ) -> BatchURLResult:
        """Process a single URL with concurrency control."""
        async with request_semaphore:
            async with batch_semaphore:
                request_id = f"JOB-{job_id}-{index + 1:02d}"
                result = await process_single_url_in_batch(
                    url_request=url_request,
                    default_format=batch_request.default_format,
                    timeout=batch_request.timeout_per_url,
                    request_id=request_id,
                    http_client=http_client,
                    pdf_semaphore=pdf_semaphore,
                )
                return result

    tasks = [
        process_with_semaphore(url_request, i)
        for i, url_request in enumerate(batch_request.urls)
    ]

    results = await asyncio.gather(*tasks, return_exceptions=False)

    batch_duration = asyncio.get_event_loop().time() - start_time

    successful_results = [r for r in results if r.success]
    failed_results = [r for r in results if not r.success]
    success_rate = (len(successful_results) / len(results)) * 100 if results else 0

    await job_manager.update_job_status(
        job_id,
        JobStatus.RUNNING,
        progress=100,
        processed_urls=len(results),
        successful_urls=len(successful_results),
        failed_urls=len(failed_results)
    )

    logger.info(
        f"[JOB-{job_id}] Batch completed: {len(successful_results)}/{len(results)} successful "
        f"({success_rate:.1f}%) in {batch_duration:.2f}s"
    )

    results_dict = [result.model_dump() for result in results]

    summary = {
        "total_requests": len(results),
        "successful_requests": len(successful_results),
        "failed_requests": len(failed_results),
        "success_rate": success_rate,
        "total_duration": batch_duration,
    }

    return results_dict, summary


@router.post("/batch")
async def submit_batch_job(
    batch_request: BatchRequest,
    request: Request,
    http_client: HTTPClientDep = None,
    job_manager: JobManagerDep = None,
    batch_semaphore: BatchSemaphoreDep = None,
    pdf_semaphore: PDFSemaphoreDep = None,
    api_key: str | None = Depends(get_api_key),
) -> JobSubmissionResponse:
    """Submit a batch processing job for background execution."""
    if job_manager is None:
        logger.warning("Batch processing requested but Redis is not configured")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    if len(batch_request.urls) > 50:
        logger.warning(f"Too many URLs requested: {len(batch_request.urls)}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error="Too many URLs in batch request. Maximum is 50 URLs.",
                error_type="validation_error",
            ).model_dump(),
        )

    try:
        request_data = batch_request.model_dump()
        job_id = await job_manager.create_job(request_data)

        await job_manager.start_background_job(
            job_id,
            process_background_batch_job,
            batch_request,
            job_manager,
            batch_semaphore,
            http_client,
            pdf_semaphore
        )

        logger.info(f"[JOB-{job_id}] Submitted batch job with {len(batch_request.urls)} URLs")

        estimated_seconds = len(batch_request.urls) * 2
        estimated_completion = None
        if estimated_seconds < 300:
            estimated_completion = (
                datetime.now(timezone.utc) + timedelta(seconds=estimated_seconds)
            ).isoformat()

        return JobSubmissionResponse(
            job_id=job_id,
            status="pending",
            created_at=datetime.now(timezone.utc).isoformat(),
            total_urls=len(batch_request.urls),
            estimated_completion=estimated_completion,
        )

    except Exception as e:
        logger.exception(f"Failed to submit batch job: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Failed to submit batch job",
                error_type="job_submission_error",
            ).model_dump(),
        )


@router.get("/jobs/{job_id}/status")
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


@router.get("/jobs/{job_id}/results")
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


@router.delete("/jobs/{job_id}")
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
            return {"success": True, "message": f"Job {job_id} cancelled successfully"}
        else:
            return {"success": False, "message": f"Job {job_id} could not be cancelled (may already be completed)"}

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

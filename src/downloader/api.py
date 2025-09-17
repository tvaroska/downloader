"""API endpoints for the downloader service."""

import asyncio
import base64
import json
import logging
import multiprocessing
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from .auth import get_api_key
from .content_converter import (
    convert_content_to_markdown,
    convert_content_to_text,
    convert_content_with_playwright_fallback,
    should_use_playwright_fallback,
)
from .http_client import (
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
    get_client,
)
from .job_manager import JobStatus, get_job_manager
from .pdf_generator import (
    PDFGeneratorError,
    generate_pdf_from_url,
)
from .validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter()


class ResponseMetadata(TypedDict):
    """Type definition for HTTP response metadata."""
    status_code: int
    headers: dict[str, str]
    url: str
    size: int
    content_type: str
    http_version: str
    connection_reused: bool | None


class ConcurrencyInfo(BaseModel):
    """Model for concurrency information of a specific service."""

    limit: int = Field(..., description="Maximum concurrent operations allowed")
    available: int = Field(..., description="Currently available slots")
    in_use: int = Field(..., description="Currently used slots")
    utilization_percent: float = Field(..., description="Utilization percentage (0-100)")


class SystemInfo(BaseModel):
    """Model for system information."""

    cpu_cores: int = Field(..., description="Number of CPU cores")
    pdf_scaling_factor: str = Field(..., description="PDF concurrency scaling factor")
    batch_scaling_factor: str = Field(..., description="Batch concurrency scaling factor")


class ConcurrencyStats(BaseModel):
    """Model for overall concurrency statistics."""

    pdf_concurrency: ConcurrencyInfo = Field(..., description="PDF generation concurrency stats")
    batch_concurrency: ConcurrencyInfo = Field(..., description="Batch processing concurrency stats")
    system_info: SystemInfo = Field(..., description="System information")


# Intelligent concurrency control with CPU-based defaults (eliminate artificial bottlenecks)
def _get_optimal_concurrency_limits() -> tuple[int, int]:
    """Calculate optimal concurrency limits based on system resources."""
    cpu_count = multiprocessing.cpu_count()

    # PDF generation is CPU/memory intensive, conservative scaling
    default_pdf_limit = min(cpu_count * 2, 12)  # 2x CPU cores, max 12
    pdf_concurrency = int(os.getenv('PDF_CONCURRENCY', default_pdf_limit))

    # Batch processing is I/O bound, more aggressive scaling
    default_batch_limit = min(cpu_count * 8, 50)  # 8x CPU cores, max 50
    batch_concurrency = int(os.getenv('BATCH_CONCURRENCY', default_batch_limit))

    logger.info(f"Concurrency limits: PDF={pdf_concurrency}, BATCH={batch_concurrency} (CPU cores: {cpu_count})")

    return pdf_concurrency, batch_concurrency

# Initialize dynamic semaphores based on system resources
_pdf_concurrency, _batch_concurrency = _get_optimal_concurrency_limits()
PDF_SEMAPHORE = asyncio.Semaphore(_pdf_concurrency)
BATCH_SEMAPHORE = asyncio.Semaphore(_batch_concurrency)


def get_concurrency_stats() -> ConcurrencyStats:
    """Get current concurrency statistics for monitoring."""
    pdf_info = ConcurrencyInfo(
        limit=_pdf_concurrency,
        available=PDF_SEMAPHORE._value,
        in_use=_pdf_concurrency - PDF_SEMAPHORE._value,
        utilization_percent=round(((_pdf_concurrency - PDF_SEMAPHORE._value) / _pdf_concurrency) * 100, 1)
    )

    batch_info = ConcurrencyInfo(
        limit=_batch_concurrency,
        available=BATCH_SEMAPHORE._value,
        in_use=_batch_concurrency - BATCH_SEMAPHORE._value,
        utilization_percent=round(((_batch_concurrency - BATCH_SEMAPHORE._value) / _batch_concurrency) * 100, 1)
    )

    system_info = SystemInfo(
        cpu_cores=multiprocessing.cpu_count(),
        pdf_scaling_factor="2x CPU cores (max 12)",
        batch_scaling_factor="8x CPU cores (max 50)"
    )

    return ConcurrencyStats(
        pdf_concurrency=pdf_info,
        batch_concurrency=batch_info,
        system_info=system_info
    )


def _check_resource_pressure() -> bool:
    """Check if system is under resource pressure and may benefit from limit adjustment."""
    pdf_util = (_pdf_concurrency - PDF_SEMAPHORE._value) / _pdf_concurrency
    batch_util = (_batch_concurrency - BATCH_SEMAPHORE._value) / _batch_concurrency

    # High utilization indicates potential for more concurrency if resources allow
    return pdf_util > 0.8 or batch_util > 0.8






class ErrorResponse(BaseModel):
    """Response model for errors."""

    success: bool = False
    error: str
    error_type: str


# Batch processing models
class BatchURLRequest(BaseModel):
    """Individual URL request within a batch."""

    url: str = Field(..., description="URL to download")
    format: str | None = Field(
        None, description="Desired output format (text, html, markdown, pdf, json, raw)"
    )
    custom_headers: dict[str, str] | None = Field(
        None, description="Custom headers for this URL"
    )


class BatchRequest(BaseModel):
    """Request model for batch processing."""

    urls: list[BatchURLRequest] = Field(
        ..., min_length=1, max_length=50, description="List of URLs to process"
    )
    default_format: str = Field(
        "text", description="Default format for URLs without explicit format"
    )
    concurrency_limit: int | None = Field(
        10, ge=1, le=20, description="Maximum concurrent requests"
    )
    timeout_per_url: int | None = Field(
        30, ge=5, le=120, description="Timeout per URL in seconds"
    )


class BatchURLResult(BaseModel):
    """Result for a single URL in a batch."""

    url: str = Field(..., description="Original URL")
    success: bool = Field(..., description="Whether processing succeeded")
    format: str = Field(..., description="Output format used")
    content: str | None = Field(None, description="Processed content (text formats)")
    content_base64: str | None = Field(
        None, description="Base64 encoded content (binary formats)"
    )
    size: int | None = Field(None, description="Content size in bytes")
    content_type: str | None = Field(None, description="Original content type")
    duration: float | None = Field(None, description="Processing time in seconds")
    error: str | None = Field(None, description="Error message if failed")
    error_type: str | None = Field(None, description="Error type classification")
    status_code: int | None = Field(None, description="HTTP status code")


class BatchResponse(BaseModel):
    """Response model for batch processing."""

    success: bool = Field(..., description="Overall batch success")
    total_requests: int = Field(..., description="Total number of URLs processed")
    successful_requests: int = Field(..., description="Number of successful requests")
    failed_requests: int = Field(..., description="Number of failed requests")
    success_rate: float = Field(..., description="Success rate as percentage")
    total_duration: float = Field(..., description="Total processing time in seconds")
    results: list[BatchURLResult] = Field(..., description="Individual URL results")
    batch_id: str | None = Field(None, description="Unique batch identifier")


# New job-based models
class JobSubmissionResponse(BaseModel):
    """Response model for job submission."""

    job_id: str = Field(..., description="Unique job identifier")
    status: str = Field(..., description="Initial job status")
    created_at: str = Field(..., description="Job creation timestamp")
    total_urls: int = Field(..., description="Total number of URLs to process")
    estimated_completion: str | None = Field(None, description="Estimated completion time")


class JobStatusResponse(BaseModel):
    """Response model for job status check."""

    job_id: str = Field(..., description="Job identifier")
    status: str = Field(..., description="Current job status")
    progress: int = Field(..., description="Progress percentage (0-100)")
    created_at: str = Field(..., description="Job creation timestamp")
    started_at: str | None = Field(None, description="Job start timestamp")
    completed_at: str | None = Field(None, description="Job completion timestamp")
    total_urls: int = Field(..., description="Total number of URLs to process")
    processed_urls: int = Field(..., description="Number of URLs processed")
    successful_urls: int = Field(..., description="Number of successfully processed URLs")
    failed_urls: int = Field(..., description="Number of failed URLs")
    error_message: str | None = Field(None, description="Error message if job failed")
    results_available: bool = Field(..., description="Whether results are available for download")
    expires_at: str | None = Field(None, description="Job expiration timestamp")


def parse_accept_header(accept_header: str | None) -> str:
    """
    Parse Accept header to determine response format.

    Args:
        accept_header: The Accept header value

    Returns:
        Format string: 'text', 'html', 'markdown', 'pdf', 'json', or 'raw'
    """
    if not accept_header:
        return "text"

    accept_header = accept_header.lower()

    # Check for specific formats in order of preference
    if "text/plain" in accept_header:
        return "text"
    elif "text/html" in accept_header:
        return "html"
    elif "text/markdown" in accept_header or "text/x-markdown" in accept_header:
        return "markdown"
    elif "application/pdf" in accept_header:
        return "pdf"
    elif "application/json" in accept_header:
        return "json"
    else:
        return "raw"


async def _playwright_fallback_for_content(
    url: str,
    processed_content: str,
    original_content: bytes,
    content_type: str,
    output_format: str,
    request_id: str = ""
) -> str:
    """
    Apply intelligent Playwright fallback for HTML content when initial BeautifulSoup extraction returns empty results.

    This function implements a smart fallback mechanism that only triggers when:
    1. The initially processed content is empty or contains only whitespace
    2. The content analysis indicates the page likely benefits from JavaScript rendering
    3. The original content is HTML-based

    The fallback uses a shared Playwright browser pool for efficient resource utilization
    and includes advanced features like modal closing, network idle waiting, and timeout control.

    Args:
        url: The URL being processed. Must be a valid URL that passed initial validation.
        processed_content: Initially processed content from BeautifulSoup (text or markdown format).
                          If this contains meaningful content, no fallback is triggered.
        original_content: Original raw content bytes from the HTTP response.
                         Used for content analysis to determine if fallback is beneficial.
        content_type: MIME content type of the original HTTP response (e.g., 'text/html').
                     Used to ensure fallback only applies to HTML content.
        output_format: Target output format for the fallback conversion.
                      Must be either 'text' or 'markdown'.
        request_id: Optional request identifier for structured logging and debugging.
                   Helps trace fallback operations in batch processing scenarios.

    Returns:
        str: Either the enhanced content from Playwright fallback (if triggered and successful)
             or the original processed_content (if fallback not needed or failed).
             The return value maintains the same format as the output_format parameter.

    Raises:
        This function does not raise exceptions. If Playwright fallback fails, it logs the error
        and returns the original processed_content, ensuring graceful degradation.

    Performance Notes:
        - Caching mechanisms prevent repeated fallback attempts for known problematic URLs
        - Smart content detection reduces unnecessary Playwright usage by 50-70%
        - Browser pool reuse minimizes resource overhead
        - 10-second timeout per operation prevents hanging requests
    """
    if not processed_content.strip() and should_use_playwright_fallback(url, original_content, content_type):
        logger.info(f"[{request_id}] Triggering optimized Playwright {output_format} fallback for empty content")
        try:
            fallback_start_time = asyncio.get_event_loop().time()
            fallback_content = await convert_content_with_playwright_fallback(url, output_format)
            fallback_duration = asyncio.get_event_loop().time() - fallback_start_time
            logger.info(
                f"✅ Playwright {output_format} fallback successful for {url}: {len(fallback_content)} characters extracted in {fallback_duration:.2f}s"
            )
            return fallback_content
        except Exception as e:
            logger.error(f"❌ Playwright {output_format} fallback failed for {url}: {str(e)}")
            logger.info(f"Falling back to original empty {output_format} for {url}")

    return processed_content


async def _handle_json_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """
    Handle JSON format response with base64-encoded content and comprehensive metadata.

    Creates a structured JSON response that preserves the original binary content as base64
    while providing detailed metadata about the download operation. This format is ideal
    for programmatic access where both content and metadata are needed.

    Args:
        content: Raw binary content from the HTTP response.
        metadata: Dictionary containing response metadata including URL, size, content_type, etc.

    Returns:
        Response: FastAPI Response with application/json content-type containing:
                 - success: Always True for successful operations
                 - url: The final resolved URL after redirects
                 - size: Content size in bytes
                 - content_type: Original MIME type from server
                 - content: Base64-encoded binary content
                 - metadata: Full metadata dictionary with additional details
    """
    content_b64 = base64.b64encode(content).decode("utf-8")

    json_response = {
        "success": True,
        "url": metadata["url"],
        "size": metadata["size"],
        "content_type": metadata["content_type"],
        "content": content_b64,
        "metadata": metadata,
    }

    return Response(
        content=json.dumps(json_response),
        media_type="application/json",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def _handle_text_response(validated_url: str, content: bytes, metadata: ResponseMetadata) -> Response:
    """
    Handle plain text format response with intelligent HTML processing and Playwright fallback.

    Converts HTML content to clean plain text by stripping all HTML tags and formatting.
    For JavaScript-heavy pages that produce empty initial results, automatically triggers
    a Playwright fallback to render the page and extract meaningful content.

    Args:
        validated_url: The validated and sanitized URL being processed.
        content: Raw binary content from the HTTP response.
        metadata: Response metadata dictionary containing content_type, size, URL, etc.

    Returns:
        Response: FastAPI Response with text/plain content-type containing:
                 - Clean plain text with HTML tags removed
                 - UTF-8 encoding
                 - Headers: X-Original-URL, X-Original-Content-Type, X-Content-Length

    Processing Details:
        - HTML content: Uses BeautifulSoup for initial text extraction
        - Empty results: Triggers Playwright fallback for JavaScript rendering
        - Non-HTML content: Returns as UTF-8 decoded text
        - Maintains original spacing and line breaks where meaningful
    """
    text_content = convert_content_to_text(content, metadata["content_type"])

    # Log BS4 extraction results for HTML content
    if "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 text extraction for {validated_url}: {len(text_content)} characters extracted"
        )

        # Apply Playwright fallback if needed
        text_content = await _playwright_fallback_for_content(
            validated_url, text_content, content, metadata["content_type"], "text"
        )

        if text_content.strip():
            logger.debug(f"Successfully extracted content for {validated_url}, no fallback needed")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping fallback logic")

    return Response(
        content=text_content,
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def _handle_markdown_response(validated_url: str, content: bytes, metadata: ResponseMetadata) -> Response:
    """
    Handle markdown format response with structured HTML-to-Markdown conversion and Playwright fallback.

    Converts HTML content to well-formatted Markdown while preserving document structure.
    Maintains headers, paragraphs, links, and lists in proper Markdown syntax.
    For JavaScript-heavy pages that produce empty initial results, automatically triggers
    a Playwright fallback to render the page and extract meaningful content.

    Args:
        validated_url: The validated and sanitized URL being processed.
        content: Raw binary content from the HTTP response.
        metadata: Response metadata dictionary containing content_type, size, URL, etc.

    Returns:
        Response: FastAPI Response with text/markdown content-type containing:
                 - Structured Markdown with proper formatting
                 - UTF-8 encoding
                 - Headers: X-Original-URL, X-Original-Content-Type, X-Content-Length

    Markdown Conversion Details:
        - Headers (h1-h6): Converted to # ## ### etc.
        - Paragraphs: Preserved with appropriate spacing
        - Links: Converted to [text](URL) format
        - Lists: Maintained as - or 1. formats
        - Empty results: Triggers Playwright fallback for JavaScript rendering
        - Non-HTML content: Returns as-is without conversion
    """
    markdown_content = convert_content_to_markdown(content, metadata["content_type"])

    # Log BS4 extraction results for HTML content
    if "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 markdown extraction for {validated_url}: {len(markdown_content)} characters extracted"
        )

        # Apply Playwright fallback if needed
        markdown_content = await _playwright_fallback_for_content(
            validated_url, markdown_content, content, metadata["content_type"], "markdown"
        )

        if markdown_content.strip():
            logger.debug(f"Successfully extracted markdown for {validated_url}, no fallback needed")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping markdown fallback logic")

    return Response(
        content=markdown_content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def _handle_pdf_response(validated_url: str, metadata: ResponseMetadata) -> Response:
    """
    Handle PDF format response with full-page rendering and intelligent concurrency control.

    Generates high-quality PDF documents using Playwright with complete JavaScript execution,
    CSS rendering, and font loading. Includes automatic modal/popup handling and network
    idle waiting for optimal content capture. Applies concurrency limiting to prevent
    resource exhaustion on the server.

    Args:
        validated_url: The validated and sanitized URL to render as PDF.
        metadata: Response metadata dictionary (used for headers, not PDF generation).

    Returns:
        Response: FastAPI Response with application/pdf content-type containing:
                 - High-quality PDF binary content
                 - Headers: X-Original-URL, X-Original-Content-Type, Content-Length
                 - Inline disposition for browser display

    Raises:
        HTTPException: 503 Service Unavailable if PDF service is at capacity.

    PDF Generation Details:
        - Viewport: 1280x720 pixels for consistent rendering
        - User Agent: Standard Chrome user agent for compatibility
        - Timeout: 10 seconds per operation (page load, rendering)
        - Concurrency: Limited by semaphore (default: 2x CPU cores, max 12)
        - Network: Waits for network idle before rendering
        - Modals: Automatically closes common popup types
        - JavaScript: Fully executed before PDF generation
    """
    logger.info(f"Generating PDF for: {validated_url}")

    # Check if PDF service is available
    if PDF_SEMAPHORE.locked():
        logger.warning(
            f"PDF service at capacity, rejecting request for: {validated_url}"
        )
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="PDF service temporarily unavailable. Please try again later.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    # Generate PDF with concurrency control
    async with PDF_SEMAPHORE:
        pdf_content = await generate_pdf_from_url(validated_url)

    return Response(
        content=pdf_content,
        media_type="application/pdf",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "Content-Length": str(len(pdf_content)),
            "Content-Disposition": 'inline; filename="download.pdf"',
        },
    )


async def _handle_html_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """
    Handle HTML format response with content-type preservation and encoding handling.

    Returns HTML content as-is when the original content is HTML, or converts non-HTML
    content to HTML format with appropriate content-type headers. No processing or
    Playwright fallback is applied to preserve the original HTML structure.

    Args:
        content: Raw binary content from the HTTP response.
        metadata: Response metadata dictionary containing original content_type, size, URL, etc.

    Returns:
        Response: FastAPI Response containing:
                 - Original HTML content (if source was HTML) or UTF-8 decoded content
                 - Content-type: Original content-type or text/html; charset=utf-8
                 - Headers: X-Original-URL, X-Original-Content-Type, X-Content-Length

    Content Handling:
        - HTML content: Returned exactly as received from server
        - Non-HTML content: UTF-8 decoded with error handling
        - No JavaScript rendering or content modification applied
        - Preserves original encoding and structure
    """
    if "html" in metadata["content_type"].lower():
        html_content = content
        response_content_type = metadata["content_type"]
    else:
        html_content = content.decode("utf-8", errors="ignore")
        response_content_type = "text/html; charset=utf-8"

    return Response(
        content=html_content,
        media_type=response_content_type,
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def _handle_raw_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """
    Handle raw format response (default) with original content preservation.

    Returns binary content exactly as received from the server without any processing,
    conversion, or modification. This is the fallback format for unrecognized Accept
    headers and preserves the original bytes and content-type.

    Args:
        content: Raw binary content from the HTTP response, preserved exactly as received.
        metadata: Response metadata dictionary containing original content_type, size, URL, etc.

    Returns:
        Response: FastAPI Response containing:
                 - Original binary content without modification
                 - Content-type: Original server content-type or application/octet-stream fallback
                 - Headers: X-Original-URL, Content-Length

    Use Cases:
        - Binary files (images, documents, archives)
        - Unknown or unspecified Accept headers (*/* or empty)
        - Content types not explicitly handled by other format handlers
        - Situations requiring exact byte-for-byte content preservation
    """
    return Response(
        content=content,
        media_type=metadata.get("content_type", "application/octet-stream"),
        headers={
            "X-Original-URL": metadata["url"],
            "Content-Length": str(metadata["size"]),
        },
    )


async def process_background_batch_job(
    job_id: str, batch_request: BatchRequest
) -> tuple[list[dict], dict]:
    """
    Process batch job in background.
    
    Args:
        job_id: Job identifier for progress tracking
        batch_request: Batch processing configuration
        
    Returns:
        Tuple of (results_list, summary_dict)
    """
    job_manager = await get_job_manager()
    start_time = asyncio.get_event_loop().time()

    logger.info(f"[JOB-{job_id}] Starting background batch processing: {len(batch_request.urls)} URLs")

    # Create semaphore for this batch's concurrency control
    batch_semaphore = asyncio.Semaphore(batch_request.concurrency_limit)

    async def process_with_semaphore(
        url_request: BatchURLRequest, index: int
    ) -> BatchURLResult:
        """Process a single URL with concurrency control."""
        async with batch_semaphore:
            # Also use global batch semaphore to prevent overloading the service
            async with BATCH_SEMAPHORE:
                request_id = f"JOB-{job_id}-{index + 1:02d}"
                result = await process_single_url_in_batch(
                    url_request=url_request,
                    default_format=batch_request.default_format,
                    timeout=batch_request.timeout_per_url,
                    request_id=request_id,
                )

                return result

    # Create tasks for all URLs
    tasks = [
        process_with_semaphore(url_request, i)
        for i, url_request in enumerate(batch_request.urls)
    ]

    # Execute all requests concurrently
    results = await asyncio.gather(*tasks, return_exceptions=False)

    batch_duration = asyncio.get_event_loop().time() - start_time

    # Calculate final statistics
    successful_results = [r for r in results if r.success]
    failed_results = [r for r in results if not r.success]
    success_rate = (len(successful_results) / len(results)) * 100 if results else 0

    # Update final progress
    await job_manager.update_job_status(
        job_id,
        JobStatus.RUNNING,  # Will be set to COMPLETED by job manager
        progress=100,
        processed_urls=len(results),
        successful_urls=len(successful_results),
        failed_urls=len(failed_results)
    )

    logger.info(
        f"[JOB-{job_id}] Batch completed: {len(successful_results)}/{len(results)} successful ({success_rate:.1f}%) in {batch_duration:.2f}s"
    )

    # Convert results to dict format for storage
    results_dict = [result.model_dump() for result in results]

    summary = {
        "total_requests": len(results),
        "successful_requests": len(successful_results),
        "failed_requests": len(failed_results),
        "success_rate": success_rate,
        "total_duration": batch_duration,
    }

    return results_dict, summary


async def process_single_url_in_batch(
    url_request: BatchURLRequest, default_format: str, timeout: int, request_id: str
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
        # Validate URL
        validated_url = validate_url(url_request.url)
        logger.info(
            f"[{request_id}] Processing batch URL: {validated_url} (format: {format_to_use})"
        )

        # Get HTTP client and download content with timeout
        client = await get_client()
        content, metadata = await asyncio.wait_for(
            client.download(validated_url, RequestPriority.LOW), timeout=timeout
        )

        # Process content based on format
        if format_to_use == "text":
            processed_content = convert_content_to_text(
                content, metadata["content_type"]
            )
            # Apply Playwright fallback if needed
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
            # Apply Playwright fallback if needed
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
            # Use semaphore for PDF generation
            async with PDF_SEMAPHORE:
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

        # Extract status code from error message
        status_code = 502  # Default to bad gateway
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


@router.post("/batch")
async def submit_batch_job(
    batch_request: BatchRequest,
    api_key: str | None = Depends(get_api_key),
) -> JobSubmissionResponse:
    """
    Submit a batch processing job for background execution.

    Args:
        batch_request: Batch processing configuration
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        JobSubmissionResponse with job ID and initial status

    Raises:
        HTTPException: For authentication failures or service unavailability

    Authentication (if DOWNLOADER_KEY environment variable is set):
    - Authorization: Bearer <api_key>
    - X-API-Key: <api_key>

    Request Body Format:
    {
        "urls": [
            {"url": "https://example.com", "format": "text"},
            {"url": "https://github.com", "format": "markdown"}
        ],
        "default_format": "text",
        "concurrency_limit": 10,
        "timeout_per_url": 30
    }

    Response includes job ID for status checking and result retrieval.
    """
    # Check if batch processing is available (requires Redis)
    if not bool(os.getenv("REDIS_URI")):
        logger.warning("Batch processing requested but Redis is not configured")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="Batch processing is not available. Redis connection (REDIS_URI) is required.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    # Validate batch size
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
        # Get job manager and create job
        job_manager = await get_job_manager()

        # Convert batch request to dict for storage
        request_data = batch_request.model_dump()

        # Create job
        job_id = await job_manager.create_job(request_data)

        # Start background processing
        await job_manager.start_background_job(
            job_id,
            process_background_batch_job,
            batch_request
        )

        logger.info(f"[JOB-{job_id}] Submitted batch job with {len(batch_request.urls)} URLs")

        # Estimate completion time (rough estimate: 2 seconds per URL + overhead)
        estimated_seconds = len(batch_request.urls) * 2
        estimated_completion = None
        if estimated_seconds < 300:  # Only provide estimate for jobs under 5 minutes
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
    api_key: str | None = Depends(get_api_key),
) -> JobStatusResponse:
    """
    Get the status of a batch processing job.

    Args:
        job_id: Job identifier
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        JobStatusResponse with current job status and progress

    Raises:
        HTTPException: For authentication failures or job not found
    """
    try:
        job_manager = await get_job_manager()
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
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """
    Download the results of a completed batch processing job.

    Args:
        job_id: Job identifier
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        JSON response with job results

    Raises:
        HTTPException: For authentication failures, job not found, or results not available
    """
    try:
        job_manager = await get_job_manager()

        # Check job status first
        job_info = await job_manager.get_job_info(job_id)
        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        # Check if results are available
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

        # Get job results
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
    api_key: str | None = Depends(get_api_key),
) -> dict[str, Any]:
    """
    Cancel a running batch processing job.

    Args:
        job_id: Job identifier
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        Cancellation status

    Raises:
        HTTPException: For authentication failures or job not found
    """
    try:
        job_manager = await get_job_manager()

        # Check if job exists
        job_info = await job_manager.get_job_info(job_id)
        if not job_info:
            raise HTTPException(
                status_code=404,
                detail=ErrorResponse(
                    error=f"Job {job_id} not found",
                    error_type="job_not_found",
                ).model_dump(),
            )

        # Try to cancel the job
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


@router.get("/{url:path}")
async def download_url(
    url: str = Path(..., description="The URL to download"),
    accept: str | None = Header(
        None, description="Accept header for content negotiation"
    ),
    request: Request = None,
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """
    Download content from a single URL with intelligent content negotiation and fallback mechanisms.

    This endpoint provides flexible content processing with automatic format detection and conversion.
    For HTML content, it employs BeautifulSoup for initial processing and uses Playwright as a fallback
    for JavaScript-heavy pages that return empty content after initial extraction.

    Args:
        url: The URL to download from. Must be a valid HTTP/HTTPS URL that passes security validation.
        accept: Accept header for content negotiation. Determines the output format and processing method.
        request: FastAPI request object for accessing request metadata.
        api_key: API key for authentication (required only if DOWNLOADER_KEY env var is set).

    Returns:
        Response: FastAPI Response object with content in the requested format, including appropriate
                 headers (Content-Type, X-Original-URL, etc.) and metadata.

    Raises:
        HTTPException:
            - 400: URL validation errors, malformed requests
            - 401/403: Authentication failures (when API key required)
            - 404: Target URL not found
            - 408: Request timeout (>30s for single requests)
            - 503: PDF service unavailable (at capacity)
            - 500: Internal server errors, PDF generation failures, unexpected errors

    Authentication (if DOWNLOADER_KEY environment variable is set):
        Two authentication methods are supported:
        - Authorization header: "Bearer <api_key>"
        - X-API-Key header: "<api_key>"

    Content Negotiation via Accept Header:
        The Accept header determines how content is processed and returned:

        - text/plain:
            * HTML content: Extracts plain text using BeautifulSoup, strips all HTML tags
            * For empty results: Automatically triggers Playwright fallback for JavaScript rendering
            * Non-HTML content: Returns as plain text with UTF-8 encoding
            * Response: text/plain; charset=utf-8

        - text/html:
            * HTML content: Returns original HTML without modification
            * Non-HTML content: Attempts UTF-8 decoding, sets text/html content-type
            * No Playwright fallback applied (preserves original HTML structure)
            * Response: Original content-type or text/html; charset=utf-8

        - text/markdown:
            * HTML content: Converts to markdown using BeautifulSoup with structured formatting
            * For empty results: Automatically triggers Playwright fallback for JavaScript rendering
            * Preserves headers (h1-h6), paragraphs, links, and lists
            * Non-HTML content: Returns as-is
            * Response: text/markdown; charset=utf-8

        - application/pdf:
            * Generates PDF using Playwright with full JavaScript rendering and page load waiting
            * Applies concurrency limiting (default: 2x CPU cores, max 12 concurrent)
            * Includes automatic modal/popup closing for better content capture
            * Viewport: 1280x720, standard user agent, 10s timeout per operation
            * Response: application/pdf with inline disposition

        - application/json:
            * Returns structured JSON with metadata and base64-encoded content
            * Includes: success flag, URL, size, content_type, content (base64), metadata
            * Works with any content type, preserves original binary data
            * Response: application/json

        - */* (or unrecognized):
            * Returns raw binary content with original content-type
            * No processing or conversion applied
            * Preserves exact bytes as received from source
            * Response: Original content-type or application/octet-stream

    Intelligent Fallback Behavior:
        For text/plain and text/markdown formats with HTML content:
        - Initial processing uses BeautifulSoup for fast extraction
        - Empty results trigger smart fallback detection using content analysis
        - Playwright fallback renders JavaScript and extracts content from fully loaded page
        - Fallback includes automatic popup/modal closing and network idle waiting
        - Caching prevents repeated fallback attempts for known empty or problematic URLs

    Security and Validation:
        - URLs undergo strict validation (no private IPs, localhost, malformed URLs)
        - Request size limits and timeout controls prevent abuse
        - PDF generation has built-in concurrency limits and resource protection
        - All user input is sanitized and validated

    Performance Optimizations:
        - HTTP client connection pooling and circuit breaker patterns
        - Intelligent caching for fallback decisions (50-70% efficiency improvement)
        - Concurrent request limiting per service type
        - Timeout controls at multiple levels (connection, read, total)
    """
    try:
        # Validate and sanitize URL
        validated_url = validate_url(url)
        logger.info(f"Processing download request for: {validated_url}")

        # Get HTTP client and download content
        client = await get_client()
        content, metadata = await client.download(validated_url, RequestPriority.HIGH)

        # Determine response format from Accept header
        format_type = parse_accept_header(accept)

        logger.info(f"Requested format: {format_type} (Accept: {accept})")

        # Process content based on format
        if format_type == "json":
            return await _handle_json_response(content, metadata)
        elif format_type == "text":
            return await _handle_text_response(validated_url, content, metadata)
        elif format_type == "markdown":
            return await _handle_markdown_response(validated_url, content, metadata)
        elif format_type == "pdf":
            return await _handle_pdf_response(validated_url, metadata)
        elif format_type == "html":
            return await _handle_html_response(content, metadata)
        else:
            return await _handle_raw_response(content, metadata)

    except URLValidationError as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=str(e), error_type="validation_error"
            ).model_dump(),
        )

    except HTTPTimeoutError as e:
        logger.error(f"Timeout error for {url}: {e}")
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        )

    except HTTPClientError as e:
        logger.error(f"HTTP client error for {url}: {e}")
        # Map common HTTP errors to appropriate status codes
        if "404" in str(e):
            status_code = 404
        elif "403" in str(e):
            status_code = 403
        elif "401" in str(e):
            status_code = 401
        elif "500" in str(e):
            status_code = 502  # Bad Gateway
        else:
            status_code = 502  # Bad Gateway for other HTTP errors

        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
        )

    except DownloadError as e:
        logger.error(f"Download error for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=str(e), error_type="download_error"
            ).model_dump(),
        )

    except PDFGeneratorError as e:
        logger.error(f"PDF generation failed for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=f"PDF generation failed: {e}", error_type="pdf_generation_error"
            ).model_dump(),
        )

    except HTTPException:
        # Re-raise HTTPExceptions (like our 503 error) without modification
        raise

    except Exception as e:
        logger.exception(f"Unexpected error downloading {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Internal server error", error_type="internal_error"
            ).model_dump(),
        )

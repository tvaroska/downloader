"""API endpoints for the downloader service."""

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from pydantic import BaseModel, Field

from .auth import get_api_key
from .http_client import DownloadError, HTTPClientError, HTTPTimeoutError, get_client
from .job_manager import JobStatus, get_job_manager
from .pdf_generator import (
    PDFGeneratorError,
    generate_pdf_from_url,
    get_shared_pdf_generator,
)
from .validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter()

# Concurrency control for PDF generation and batch processing
PDF_SEMAPHORE = asyncio.Semaphore(5)  # Max 5 concurrent PDF generations
BATCH_SEMAPHORE = asyncio.Semaphore(20)  # Max 20 concurrent batch downloads


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


async def convert_content_with_playwright_fallback(
    url: str, output_format: str = "text"
) -> str:
    """
    Convert content using Playwright to get rendered HTML.
    This is used as a fallback when BeautifulSoup returns empty content.

    Args:
        url: The URL to fetch and convert
        output_format: Either "text" or "markdown" for the output format

    Returns:
        Text or markdown representation with article content extracted

    Raises:
        Exception: If Playwright conversion fails
    """
    try:
        logger.info(f"🔄 Starting Playwright {output_format} fallback for {url}")
        generator = get_shared_pdf_generator()
        if not generator or not generator.pool:
            raise Exception("PDF generator pool not initialized")

        browser = await generator.pool.get_browser()
        context = None
        page = None

        try:
            # Create isolated context for this request
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 720},
                ignore_https_errors=False,
                java_script_enabled=True,
                bypass_csp=False,
            )

            # Create page in isolated context
            page = await context.new_page()

            logger.info(f"🌐 Loading page with Playwright: {url}")
            # Navigate to page with timeout
            response = await page.goto(url, wait_until="networkidle", timeout=30000)

            if not response or response.status >= 400:
                raise Exception(f"Failed to load page: {url}")

            logger.debug(f"Page loaded, waiting for network idle: {url}")
            # Wait for page to be fully loaded
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Try to close any signup boxes/modals
            try:
                close_selectors = [
                    '[aria-label="close"]',
                    '[title="Close"]',
                    '[aria-label="Close"]',
                    '[title="close"]',
                ]
                for selector in close_selectors:
                    close_buttons = await page.query_selector_all(selector)
                    for button in close_buttons:
                        try:
                            await button.click(timeout=1000)
                            logger.debug(
                                f"Closed modal/popup with selector: {selector}"
                            )
                            await page.wait_for_timeout(500)  # Brief wait after closing
                        except Exception:
                            pass  # Ignore if click fails
            except Exception:
                pass  # Ignore any errors during modal closing

            logger.debug(f"Extracting rendered HTML content for {output_format}: {url}")
            # Get the rendered HTML content
            html_content = await page.content()

            emoji = "📄" if output_format == "text" else "📝"
            logger.info(
                f"{emoji} Processing rendered HTML content ({len(html_content)} chars) for {output_format} extraction: {url}"
            )
            # Convert HTML using BeautifulSoup
            soup = BeautifulSoup(html_content, "lxml")

            # Remove unwanted elements
            for element in soup(
                [
                    "script",
                    "style",
                    "nav",
                    "header",
                    "footer",
                    "aside",
                    "menu",
                    "form",
                    "iframe",
                    "noscript",
                ]
            ):
                element.decompose()

            # Try to find main content in common article containers
            main_content = None
            for selector in [
                "article",
                "main",
                '[role="main"]',
                ".content",
                ".post-content",
                ".entry-content",
                ".article-content",
            ]:
                main_content = soup.select_one(selector)
                if main_content:
                    break

            # If no main content container found, use body or entire document
            if not main_content:
                main_content = soup.find("body") or soup

            if output_format == "markdown":
                # Convert to markdown
                markdown_parts = []

                for element in main_content.find_all(
                    ["h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "ul", "ol", "li"]
                ):
                    if element.name.startswith("h"):
                        level = int(element.name[1])
                        markdown_parts.append(
                            "#" * level + " " + element.get_text(strip=True)
                        )
                    elif element.name == "p":
                        text_content = element.get_text(strip=True)
                        if text_content:
                            markdown_parts.append(text_content)
                    elif element.name == "a" and element.get("href"):
                        link_text = element.get_text(strip=True)
                        href = element.get("href")
                        if link_text and href:
                            markdown_parts.append(f"[{link_text}]({href})")
                    elif element.name in ["ul", "ol"]:
                        for li in element.find_all("li", recursive=False):
                            li_text = li.get_text(strip=True)
                            if li_text:
                                prefix = "- " if element.name == "ul" else "1. "
                                markdown_parts.append(prefix + li_text)

                # If no structured content found, fall back to simple text extraction
                if not markdown_parts:
                    text = main_content.get_text(separator="\n", strip=True)
                    text = re.sub(r"\n\s*\n+", "\n\n", text)
                    return text.strip()

                # Join markdown parts with appropriate spacing
                text = "\n\n".join(markdown_parts)

                # Clean up excessive whitespace
                text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
                text = text.strip()
            else:
                # Extract text with proper spacing
                text = main_content.get_text(separator=" ", strip=True)

                # Clean up excessive whitespace
                text = re.sub(r"\s+", " ", text).strip()

            return text

        finally:
            # Cleanup in reverse order
            if context:
                await context.close()
            if generator.pool:
                await generator.pool.release_browser(browser)

    except Exception as e:
        logger.error(f"Playwright {output_format} fallback failed for {url}: {e}")
        raise


def convert_content_to_text(content: bytes, content_type: str) -> str:
    """
    Convert content to plain text, extracting main article content from HTML.

    Args:
        content: Raw content bytes
        content_type: Original content type

    Returns:
        Plain text representation with article content extracted
    """
    try:
        # Try to decode as text
        text = content.decode("utf-8", errors="ignore")

        # If it's HTML, use BeautifulSoup to extract article content
        if "html" in content_type.lower():
            soup = BeautifulSoup(text, "lxml")

            # Remove unwanted elements
            for element in soup(
                [
                    "script",
                    "style",
                    "nav",
                    "header",
                    "footer",
                    "aside",
                    "menu",
                    "form",
                    "iframe",
                    "noscript",
                ]
            ):
                element.decompose()

            # Try to find main content in common article containers
            main_content = None
            for selector in [
                "article",
                "main",
                '[role="main"]',
                ".content",
                ".post-content",
                ".entry-content",
                ".article-content",
            ]:
                main_content = soup.select_one(selector)
                if main_content:
                    break

            # If no main content container found, use body or entire document
            if not main_content:
                main_content = soup.find("body") or soup

            # Extract text with proper spacing
            text = main_content.get_text(separator=" ", strip=True)

            # Clean up excessive whitespace
            text = re.sub(r"\s+", " ", text).strip()

        return text
    except Exception:
        return content.decode("utf-8", errors="replace")


def convert_content_to_markdown(content: bytes, content_type: str) -> str:
    """
    Convert content to markdown format, extracting main article content from HTML.

    Args:
        content: Raw content bytes
        content_type: Original content type

    Returns:
        Markdown representation with article content extracted
    """
    try:
        text = content.decode("utf-8", errors="ignore")

        # If it's HTML, use BeautifulSoup for better markdown conversion
        if "html" in content_type.lower():
            soup = BeautifulSoup(text, "lxml")

            # Remove unwanted elements
            for element in soup(
                [
                    "script",
                    "style",
                    "nav",
                    "header",
                    "footer",
                    "aside",
                    "menu",
                    "form",
                    "iframe",
                    "noscript",
                ]
            ):
                element.decompose()

            # Try to find main content in common article containers
            main_content = None
            for selector in [
                "article",
                "main",
                '[role="main"]',
                ".content",
                ".post-content",
                ".entry-content",
                ".article-content",
            ]:
                main_content = soup.select_one(selector)
                if main_content:
                    break

            # If no main content container found, use body or entire document
            if not main_content:
                main_content = soup.find("body") or soup

            # Convert to markdown
            markdown_parts = []

            for element in main_content.find_all(
                ["h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "ul", "ol", "li"]
            ):
                if element.name.startswith("h"):
                    level = int(element.name[1])
                    markdown_parts.append(
                        "#" * level + " " + element.get_text(strip=True)
                    )
                elif element.name == "p":
                    text_content = element.get_text(strip=True)
                    if text_content:
                        markdown_parts.append(text_content)
                elif element.name == "a" and element.get("href"):
                    link_text = element.get_text(strip=True)
                    href = element.get("href")
                    if link_text and href:
                        markdown_parts.append(f"[{link_text}]({href})")
                elif element.name in ["ul", "ol"]:
                    for li in element.find_all("li", recursive=False):
                        li_text = li.get_text(strip=True)
                        if li_text:
                            prefix = "- " if element.name == "ul" else "1. "
                            markdown_parts.append(prefix + li_text)

            # If no structured content found, fall back to simple text extraction
            if not markdown_parts:
                text = main_content.get_text(separator="\n", strip=True)
                text = re.sub(r"\n\s*\n+", "\n\n", text)
                return text.strip()

            # Join markdown parts with appropriate spacing
            text = "\n\n".join(markdown_parts)

            # Clean up excessive whitespace
            text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
            text = text.strip()

        return text
    except Exception:
        return content.decode("utf-8", errors="replace")


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

    async def process_with_semaphore_and_progress(
        url_request: BatchURLRequest, index: int
    ) -> BatchURLResult:
        """Process a single URL with concurrency control and progress updates."""
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

                # Update progress
                processed_count = index + 1
                progress = int((processed_count / len(batch_request.urls)) * 100)
                successful_count = sum(1 for i in range(processed_count) if i == index and result.success)
                failed_count = sum(1 for i in range(processed_count) if i == index and not result.success)

                await job_manager.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    progress=progress,
                    processed_urls=processed_count,
                    successful_urls=successful_count,
                    failed_urls=failed_count
                )

                return result

    # Create tasks for all URLs
    tasks = [
        process_with_semaphore_and_progress(url_request, i)
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
            client.download(validated_url), timeout=timeout
        )

        # Process content based on format
        if format_to_use == "text":
            processed_content = convert_content_to_text(
                content, metadata["content_type"]
            )
            # Handle Playwright fallback for empty HTML content
            if (
                "html" in metadata["content_type"].lower()
                and not processed_content.strip()
            ):
                logger.info(
                    f"[{request_id}] Triggering Playwright fallback for empty content"
                )
                try:
                    processed_content = await convert_content_with_playwright_fallback(
                        validated_url, "text"
                    )
                except Exception as e:
                    logger.warning(f"[{request_id}] Playwright fallback failed: {e}")

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
            # Handle Playwright fallback for empty HTML content
            if (
                "html" in metadata["content_type"].lower()
                and not processed_content.strip()
            ):
                logger.info(
                    f"[{request_id}] Triggering Playwright markdown fallback for empty content"
                )
                try:
                    processed_content = await convert_content_with_playwright_fallback(
                        validated_url, "markdown"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{request_id}] Playwright markdown fallback failed: {e}"
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
) -> dict:
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
    Download content from a single URL with content negotiation.

    Args:
        url: The URL to download from
        accept: Accept header for content negotiation
        request: FastAPI request object
        api_key: API key for authentication (if DOWNLOADER_KEY env var is set)

    Returns:
        Response with content in requested format

    Raises:
        HTTPException: For various error conditions including authentication failures

    Authentication (if DOWNLOADER_KEY environment variable is set):
    - Authorization: Bearer <api_key>
    - X-API-Key: <api_key>

    Content Negotiation via Accept Header:
    - text/plain: Returns plain text (HTML tags stripped)
    - text/html: Returns original HTML content
    - text/markdown: Returns markdown conversion of HTML
    - application/pdf: Returns PDF generated via Playwright with JavaScript rendering
    - application/json: Returns JSON with base64 content and metadata
    - */*: Returns raw bytes with original content-type
    """
    try:
        # Validate and sanitize URL
        validated_url = validate_url(url)
        logger.info(f"Processing download request for: {validated_url}")

        # Get HTTP client and download content
        client = await get_client()
        content, metadata = await client.download(validated_url)

        # Determine response format from Accept header
        format_type = parse_accept_header(accept)

        logger.info(f"Requested format: {format_type} (Accept: {accept})")

        if format_type == "json":
            # JSON response with base64 content and metadata
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

        elif format_type == "text":
            # Plain text response
            text_content = convert_content_to_text(content, metadata["content_type"])

            # Log BS4 extraction results for HTML content
            if "html" in metadata["content_type"].lower():
                logger.info(
                    f"BS4 text extraction for {validated_url}: {len(text_content)} characters extracted"
                )

                # Check if BeautifulSoup returned empty text for HTML content and use Playwright fallback
                if not text_content.strip():
                    logger.warning(
                        f"BS4 returned empty text for HTML content at {validated_url} (content size: {len(content)} bytes)"
                    )
                    logger.info(f"Triggering Playwright fallback for {validated_url}")
                    try:
                        fallback_start_time = asyncio.get_event_loop().time()
                        text_content = await convert_content_with_playwright_fallback(
                            validated_url, "text"
                        )
                        fallback_duration = (
                            asyncio.get_event_loop().time() - fallback_start_time
                        )
                        logger.info(
                            f"✅ Playwright fallback successful for {validated_url}: {len(text_content)} characters extracted in {fallback_duration:.2f}s"
                        )
                    except Exception as e:
                        logger.error(
                            f"❌ Playwright fallback failed for {validated_url}: {str(e)}"
                        )
                        logger.info(
                            f"Falling back to original empty text for {validated_url}"
                        )
                        # Keep the original empty text if Playwright fails
                else:
                    logger.debug(
                        f"BS4 successfully extracted content for {validated_url}, no fallback needed"
                    )
            else:
                logger.debug(
                    f"Non-HTML content for {validated_url}, skipping fallback logic"
                )

            return Response(
                content=text_content,
                media_type="text/plain; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )

        elif format_type == "markdown":
            # Markdown response
            markdown_content = convert_content_to_markdown(
                content, metadata["content_type"]
            )

            # Log BS4 extraction results for HTML content
            if "html" in metadata["content_type"].lower():
                logger.info(
                    f"BS4 markdown extraction for {validated_url}: {len(markdown_content)} characters extracted"
                )

                # Check if BeautifulSoup returned empty markdown for HTML content and use Playwright fallback
                if not markdown_content.strip():
                    logger.warning(
                        f"BS4 returned empty markdown for HTML content at {validated_url} (content size: {len(content)} bytes)"
                    )
                    logger.info(
                        f"Triggering Playwright markdown fallback for {validated_url}"
                    )
                    try:
                        fallback_start_time = asyncio.get_event_loop().time()
                        markdown_content = (
                            await convert_content_with_playwright_fallback(
                                validated_url, "markdown"
                            )
                        )
                        fallback_duration = (
                            asyncio.get_event_loop().time() - fallback_start_time
                        )
                        logger.info(
                            f"✅ Playwright markdown fallback successful for {validated_url}: {len(markdown_content)} characters extracted in {fallback_duration:.2f}s"
                        )
                    except Exception as e:
                        logger.error(
                            f"❌ Playwright markdown fallback failed for {validated_url}: {str(e)}"
                        )
                        logger.info(
                            f"Falling back to original empty markdown for {validated_url}"
                        )
                        # Keep the original empty markdown if Playwright fails
                else:
                    logger.debug(
                        f"BS4 successfully extracted markdown for {validated_url}, no fallback needed"
                    )
            else:
                logger.debug(
                    f"Non-HTML content for {validated_url}, skipping markdown fallback logic"
                )

            return Response(
                content=markdown_content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                },
            )

        elif format_type == "pdf":
            # PDF response - generate PDF using Playwright with concurrency control
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

        elif format_type == "html":
            # HTML response (if original was HTML, return as-is; otherwise decode)
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

        else:
            # Raw response (default)
            return Response(
                content=content,
                media_type=metadata.get("content_type", "application/octet-stream"),
                headers={
                    "X-Original-URL": metadata["url"],
                    "Content-Length": str(metadata["size"]),
                },
            )

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

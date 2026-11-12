"""Batch download job execution engine shared by in-process API and split workers."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from typing import Any
from urllib.parse import urlparse

from ..browser.actions import BrowserActionError
from ..config import get_settings
from ..content_converter import (
    convert_content_to_markdown,
    convert_content_to_text,
    render_html_with_playwright,
)
from ..crawling import DomainThrottle, RobotsChecker, RobotsDisallowedError
from ..http_client import (
    ContentTooLargeError,
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
)
from ..job_manager import JobStatus
from ..models.responses import BatchRequest, BatchURLRequest, BatchURLResult
from ..pdf_generator import (
    PDFGeneratorError,
    generate_pdf_from_url,
    generate_screenshot_from_url,
)
from ..transformers.ocr import (
    OCRError,
    OCRUnavailableError,
    extract_text_from_image,
    format_ocr_markdown,
    is_ocr_image_content_type,
)
from ..validation import URLValidationError, sanitize_custom_headers, validate_url_async
from .content_processor import _playwright_fallback_for_content
from .webhooks import deliver_webhook

logger = logging.getLogger(__name__)


def _resolve_batch_hook(name: str, default: Any) -> Any:
    """Resolve hooks re-exported on src.downloader.routes.batch for monkeypatch compatibility."""
    batch_mod = sys.modules.get("src.downloader.routes.batch")
    if batch_mod is not None:
        return getattr(batch_mod, name, default)
    return default


async def process_single_url_in_batch(
    url_request: BatchURLRequest,
    default_format: str,
    timeout: int,
    request_id: str,
    http_client: Any,
    pdf_semaphore: asyncio.Semaphore,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
) -> BatchURLResult:
    """Process a single URL within a batch request."""
    start_time = asyncio.get_event_loop().time()
    format_to_use = url_request.format or default_format
    fallback_fn = _resolve_batch_hook(
        "_playwright_fallback_for_content", _playwright_fallback_for_content
    )
    validate_fn = _resolve_batch_hook("validate_url_async", validate_url_async)
    pdf_fn = _resolve_batch_hook("generate_pdf_from_url", generate_pdf_from_url)

    try:
        validated_url = await validate_fn(url_request.url)
        sanitized_headers = sanitize_custom_headers(url_request.custom_headers)
        logger.info(
            f"[{request_id}] Processing batch URL: {validated_url} (format: {format_to_use})"
        )

        settings = get_settings()
        if settings.crawling.respect_robots_txt:
            active_checker = robots_checker
            if active_checker is None:
                from ..main import app as _app

                active_checker = getattr(_app.state, "robots_checker", None)
            if active_checker is not None:
                effective_ua = next(
                    (v for k, v in (sanitized_headers or {}).items() if k.lower() == "user-agent"),
                    settings.crawling.user_agent,
                )
                crawl_delay = await active_checker.check_allowed(validated_url, effective_ua)
                active_throttle = domain_throttle
                if active_throttle is None:
                    from ..main import app as _app

                    active_throttle = getattr(_app.state, "domain_throttle", None)
                if active_throttle is not None:
                    await active_throttle.wait_if_needed(
                        urlparse(validated_url).netloc, crawl_delay
                    )

        if format_to_use == "pdf":
            async with pdf_semaphore:
                if url_request.actions:
                    pdf_content = await asyncio.wait_for(
                        pdf_fn(validated_url, actions=url_request.actions),
                        timeout=timeout,
                    )
                else:
                    pdf_content = await asyncio.wait_for(
                        pdf_fn(validated_url),
                        timeout=timeout,
                    )

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

        if format_to_use == "screenshot":
            shot_options = {
                "type": url_request.screenshot_format,
                "full_page": url_request.full_page,
            }
            async with pdf_semaphore:
                shot_bytes = await asyncio.wait_for(
                    generate_screenshot_from_url(
                        validated_url,
                        shot_options,
                        actions=url_request.actions,
                    ),
                    timeout=timeout,
                )

            shot_mime = "image/jpeg" if url_request.screenshot_format == "jpeg" else "image/png"
            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content_base64=base64.b64encode(shot_bytes).decode("utf-8"),
                size=len(shot_bytes),
                content_type=shot_mime,
                duration=duration,
                status_code=200,
            )

        if url_request.actions and format_to_use in {"html", "text", "markdown"}:
            async with pdf_semaphore:
                rendered_html = await asyncio.wait_for(
                    render_html_with_playwright(validated_url, actions=url_request.actions),
                    timeout=timeout,
                )
            if format_to_use == "html":
                processed = rendered_html.decode("utf-8", errors="ignore")
            elif format_to_use == "markdown":
                processed = convert_content_to_markdown(rendered_html, "text/html")
            else:
                processed = convert_content_to_text(rendered_html, "text/html")
            duration = asyncio.get_event_loop().time() - start_time
            return BatchURLResult(
                url=url_request.url,
                success=True,
                format=format_to_use,
                content=processed,
                size=len(processed.encode("utf-8")),
                content_type="text/html; charset=utf-8",
                duration=duration,
                status_code=200,
            )

        if sanitized_headers:
            download_coro = http_client.download(
                validated_url, RequestPriority.LOW, headers=sanitized_headers
            )
        else:
            download_coro = http_client.download(validated_url, RequestPriority.LOW)

        content, metadata = await asyncio.wait_for(download_coro, timeout=timeout)

        if format_to_use == "text":
            if is_ocr_image_content_type(metadata["content_type"]):
                processed_content = await extract_text_from_image(
                    content, language=url_request.ocr_language
                )
            else:
                processed_content = convert_content_to_text(content, metadata["content_type"])
                if not processed_content.strip():
                    async with pdf_semaphore:
                        processed_content = await fallback_fn(
                            validated_url,
                            processed_content,
                            content,
                            metadata["content_type"],
                            "text",
                            request_id,
                        )
                else:
                    processed_content = await fallback_fn(
                        validated_url,
                        processed_content,
                        content,
                        metadata["content_type"],
                        "text",
                        request_id,
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
            if is_ocr_image_content_type(metadata["content_type"]):
                ocr_text = await extract_text_from_image(content, language=url_request.ocr_language)
                processed_content = format_ocr_markdown(ocr_text, metadata["url"])
            else:
                processed_content = convert_content_to_markdown(content, metadata["content_type"])
                if not processed_content.strip():
                    async with pdf_semaphore:
                        processed_content = await fallback_fn(
                            validated_url,
                            processed_content,
                            content,
                            metadata["content_type"],
                            "markdown",
                            request_id,
                        )
                else:
                    processed_content = await fallback_fn(
                        validated_url,
                        processed_content,
                        content,
                        metadata["content_type"],
                        "markdown",
                        request_id,
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

        else:
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

    except RobotsDisallowedError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="robots_disallowed",
            status_code=403,
        )

    except URLValidationError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="validation_error",
            status_code=400,
        )

    except ContentTooLargeError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="content_too_large",
            status_code=413,
        )

    except TimeoutError:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=f"Request timeout after {timeout} seconds",
            error_type="timeout_error",
            status_code=408,
        )

    except HTTPTimeoutError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="timeout_error",
            status_code=408,
        )

    except HTTPClientError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="http_error",
            status_code=e.status_code if e.status_code else 502,
        )

    except OCRUnavailableError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="ocr_unavailable",
            status_code=503,
        )

    except OCRError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="ocr_error",
            status_code=422,
        )

    except BrowserActionError as e:
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error=str(e),
            error_type="browser_action_error",
            status_code=400,
        )

    except PDFGeneratorError as e:
        duration = asyncio.get_event_loop().time() - start_time
        if e.status_code is not None and 400 <= e.status_code < 500:
            return BatchURLResult(
                url=url_request.url,
                success=False,
                format=format_to_use,
                duration=duration,
                error=str(e),
                error_type="http_error",
                status_code=e.status_code,
            )
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
        logger.exception(f"[{request_id}] Unexpected error: {e}")
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=asyncio.get_event_loop().time() - start_time,
            error="Internal server error",
            error_type="internal_error",
            status_code=500,
        )


async def process_background_batch_job(
    job_id: str,
    batch_request: BatchRequest,
    job_manager: Any,
    batch_semaphore: asyncio.Semaphore,
    http_client: Any,
    pdf_semaphore: asyncio.Semaphore,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Process a batch job in the background and optionally fire a completion webhook."""
    start_time = asyncio.get_event_loop().time()
    logger.info(
        f"[JOB-{job_id}] Starting background batch processing: {len(batch_request.urls)} URLs"
    )

    settings = get_settings()
    concurrency_limit = (
        batch_request.concurrency_limit
        if batch_request.concurrency_limit is not None
        else min(10, settings.batch.concurrency)
    )
    timeout_per_url = (
        batch_request.timeout_per_url
        if batch_request.timeout_per_url is not None
        else settings.batch.default_timeout_per_url
    )
    request_semaphore = asyncio.Semaphore(concurrency_limit)
    progress_lock = asyncio.Lock()
    total_urls = max(1, len(batch_request.urls))
    processed_count = 0
    success_count = 0
    fail_count = 0

    async def process_with_semaphore(url_request: BatchURLRequest, index: int) -> BatchURLResult:
        nonlocal processed_count, success_count, fail_count
        async with request_semaphore:
            async with batch_semaphore:
                request_id = f"JOB-{job_id}-{index + 1:02d}"
                result = await process_single_url_in_batch(
                    url_request=url_request,
                    default_format=batch_request.default_format,
                    timeout=timeout_per_url,
                    request_id=request_id,
                    http_client=http_client,
                    pdf_semaphore=pdf_semaphore,
                    robots_checker=robots_checker,
                    domain_throttle=domain_throttle,
                )
        async with progress_lock:
            processed_count += 1
            if result.success:
                success_count += 1
            else:
                fail_count += 1
            try:
                await job_manager.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    progress=int(processed_count * 100 / total_urls),
                    processed_urls=processed_count,
                    successful_urls=success_count,
                    failed_urls=fail_count,
                )
            except Exception as exc:
                logger.warning(f"[JOB-{job_id}] Incremental progress update failed: {exc}")
        return result

    tasks = [
        process_with_semaphore(url_request, i) for i, url_request in enumerate(batch_request.urls)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)

    batch_duration = asyncio.get_event_loop().time() - start_time
    successful_results = [r for r in results if r.success]
    failed_results = [r for r in results if not r.success]
    success_rate = (len(successful_results) / len(results)) * 100 if results else 0

    if not results:
        await job_manager.update_job_status(
            job_id,
            JobStatus.RUNNING,
            progress=100,
            processed_urls=0,
            successful_urls=0,
            failed_urls=0,
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


async def notify_batch_webhook(
    job_id: str,
    batch_request: BatchRequest,
    status: JobStatus,
    *,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Deliver a batch completion or failure webhook after Redis state is persisted."""
    if not batch_request.webhook_url:
        return

    event = "batch.completed" if status == JobStatus.COMPLETED else "batch.failed"
    payload: dict[str, Any] = {
        "event": event,
        "job_id": job_id,
        "status": status.value,
        "results_endpoint": f"/jobs/{job_id}/results",
    }
    if summary is not None:
        payload["summary"] = summary
    if error is not None:
        payload["error"] = error

    await deliver_webhook(
        batch_request.webhook_url,
        event,
        payload,
        secret=batch_request.webhook_secret,
        settings=get_settings(),
    )

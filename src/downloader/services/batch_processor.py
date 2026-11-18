"""Batch download job execution engine delegating format conversion to `convert()`."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from dataclasses import dataclass
from typing import Any, cast

from ..config import Settings, get_settings
from ..crawling import DomainThrottle, RobotsChecker
from ..errors import classify_domain_error, log_classified_error
from ..http_client import HTTPClient
from ..job_manager import JobStatus
from ..models.responses import BatchRequest, BatchURLRequest, BatchURLResult, ResponseMetadata
from ..ports import BrowserRenderer, JobStore, LocalBrowserRenderer
from ..validation import canonical_host, sanitize_custom_headers, validate_url_async
from .content_processor import (
    ConversionDeps,
    ConversionRequest,
    ConversionResult,
    OutputFormat,
    convert,
)
from .webhooks import deliver_webhook

logger = logging.getLogger(__name__)

_BINARY_BATCH_FORMATS = frozenset({"pdf", "screenshot", "screenshot_jpeg", "raw"})


@dataclass(frozen=True)
class BatchExecutionDeps:
    """Explicit collaborator bundle for batch URL processing (zero globals or test hooks)."""

    http_client: Any
    renderer: BrowserRenderer
    robots_checker: RobotsChecker | None = None
    domain_throttle: DomainThrottle | None = None
    settings: Settings | None = None


async def _enforce_batch_crawling_policy(
    validated_url: str,
    sanitized_headers: dict[str, str] | None,
    deps: BatchExecutionDeps,
    cfg: Settings,
) -> None:
    if not cfg.crawling.respect_robots_txt or deps.robots_checker is None:
        return
    effective_ua = next(
        (v for k, v in (sanitized_headers or {}).items() if k.lower() == "user-agent"),
        cfg.crawling.user_agent,
    )
    crawl_delay = await deps.robots_checker.check_allowed(validated_url, effective_ua)
    if deps.domain_throttle is not None:
        await deps.domain_throttle.wait_if_needed(canonical_host(validated_url), crawl_delay)


def _make_batch_http_fetcher(
    http_client: Any,
    validated_url: str,
    sanitized_headers: dict[str, str] | None,
    format_to_use: str,
):
    cached: tuple[bytes, ResponseMetadata] | None = None
    require_text = format_to_use in {"text", "markdown", "html"}
    allow_ocr = format_to_use in {"text", "markdown"}

    async def _fetch() -> tuple[bytes, ResponseMetadata]:
        nonlocal cached
        if cached is None:
            if isinstance(http_client, HTTPClient):
                cached = await http_client.download(
                    validated_url,
                    headers=sanitized_headers,
                    require_text_convertible=require_text,
                    allow_ocr_images=allow_ocr,
                    target_format=format_to_use,
                )
            elif sanitized_headers:
                cached = await http_client.download(validated_url, headers=sanitized_headers)
            else:
                cached = await http_client.download(validated_url)
        return cached

    return _fetch


def _build_batch_success_result(
    raw_url: str,
    format_to_use: str,
    duration: float,
    conv_res: ConversionResult,
) -> BatchURLResult:
    if format_to_use in _BINARY_BATCH_FORMATS:
        out_ct = (
            conv_res.media_type
            if format_to_use in {"pdf", "screenshot", "screenshot_jpeg"}
            else conv_res.original_content_type
        )
        return BatchURLResult(
            url=raw_url,
            success=True,
            format=format_to_use,
            content_base64=base64.b64encode(conv_res.body).decode("utf-8"),
            size=len(conv_res.body),
            content_type=out_ct,
            duration=duration,
            status_code=200,
        )

    text_content = conv_res.body.decode("utf-8", errors="replace")
    out_ct = "application/json" if format_to_use == "json" else conv_res.original_content_type
    return BatchURLResult(
        url=raw_url,
        success=True,
        format=format_to_use,
        content=text_content,
        size=len(conv_res.body),
        content_type=out_ct,
        duration=duration,
        status_code=200,
    )


async def process_single_url_in_batch(  # noqa: PLR0913
    url_request: BatchURLRequest,
    default_format: str,
    timeout: int,
    request_id: str,
    http_client: Any,
    pdf_semaphore: asyncio.Semaphore | None = None,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
    *,
    renderer: BrowserRenderer | None = None,
    settings: Settings | None = None,
) -> BatchURLResult:
    """Process one batch URL through `convert()` and `classify_domain_error()`."""
    started = time.perf_counter()
    format_to_use = url_request.format or default_format
    cfg = settings or get_settings()
    active_renderer = renderer or LocalBrowserRenderer(pdf_semaphore)
    exec_deps = BatchExecutionDeps(
        http_client=http_client,
        renderer=active_renderer,
        robots_checker=robots_checker,
        domain_throttle=domain_throttle,
        settings=cfg,
    )

    try:
        validated_url = await validate_url_async(url_request.url, cfg)
        sanitized_headers = sanitize_custom_headers(url_request.custom_headers)
        logger.info(
            f"[{request_id}] Processing batch URL: {validated_url} (format: {format_to_use})"
        )

        await _enforce_batch_crawling_policy(validated_url, sanitized_headers, exec_deps, cfg)

        conv_fmt: OutputFormat = (
            cast(OutputFormat, format_to_use)
            if format_to_use in {"text", "markdown", "html", "pdf", "screenshot", "json"}
            else "raw"
        )
        conv_req = ConversionRequest(
            url=validated_url,
            output_format=conv_fmt,
            force_render=bool(url_request.actions) and conv_fmt in {"html", "text", "markdown"},
            actions=url_request.actions or None,
            full_page=url_request.full_page,
            screenshot_format=url_request.screenshot_format,
            ocr_language=url_request.ocr_language,
        )
        conv_deps = ConversionDeps(
            renderer=active_renderer,
            http_fetcher=_make_batch_http_fetcher(
                http_client, validated_url, sanitized_headers, format_to_use
            ),
        )
        conv_res = await asyncio.wait_for(convert(conv_req, conv_deps), timeout=timeout)
        return _build_batch_success_result(
            url_request.url,
            format_to_use,
            time.perf_counter() - started,
            conv_res,
        )
    except Exception as exc:  # noqa: BLE001
        # Isolate per-URL failures so one failing target never aborts sibling URLs in the batch.
        classified = classify_domain_error(exc, context="batch", batch_timeout=timeout)
        log_classified_error(logger, f"[{request_id}] {url_request.url}", exc, classified)
        return BatchURLResult(
            url=url_request.url,
            success=False,
            format=format_to_use,
            duration=time.perf_counter() - started,
            error=classified.message,
            error_type=classified.error_type,
            status_code=classified.status_code,
        )


async def process_background_batch_job(  # noqa: PLR0913
    job_id: str,
    batch_request: BatchRequest,
    job_manager: JobStore,
    batch_semaphore: asyncio.Semaphore,
    http_client: Any,
    pdf_semaphore: asyncio.Semaphore | None = None,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
    *,
    renderer: BrowserRenderer | None = None,
    settings: Settings | None = None,
    max_result_bytes: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Execute a background batch job with bounded concurrency and incremental `JobStore` updates."""
    started = time.perf_counter()
    cfg = settings or get_settings()
    active_renderer = renderer or LocalBrowserRenderer(pdf_semaphore)
    max_aggregate_bytes = (
        max_result_bytes if max_result_bytes is not None else cfg.content.max_download_size
    )
    concurrency_limit = (
        batch_request.concurrency_limit
        if batch_request.concurrency_limit is not None
        else min(10, cfg.batch.concurrency)
    )
    timeout_per_url = (
        batch_request.timeout_per_url
        if batch_request.timeout_per_url is not None
        else cfg.batch.default_timeout_per_url
    )

    request_semaphore = asyncio.Semaphore(concurrency_limit)
    progress_lock = asyncio.Lock()
    total_urls = max(1, len(batch_request.urls))
    counts = {"processed": 0, "success": 0, "failed": 0, "result_bytes": 0}

    async def _run_item(url_request: BatchURLRequest, index: int) -> BatchURLResult:
        async with request_semaphore, batch_semaphore:
            current_job = await job_manager.get_job_info(job_id)
            if current_job is not None and current_job.status == JobStatus.CANCELLED:
                raise asyncio.CancelledError(f"Job {job_id} was cancelled")

            result = await process_single_url_in_batch(
                url_request=url_request,
                default_format=batch_request.default_format,
                timeout=timeout_per_url,
                request_id=f"JOB-{job_id}-{index + 1:02d}",
                http_client=http_client,
                pdf_semaphore=pdf_semaphore,
                robots_checker=robots_checker,
                domain_throttle=domain_throttle,
                renderer=active_renderer,
                settings=cfg,
            )
        async with progress_lock:
            result_bytes = len(result.model_dump_json().encode("utf-8"))
            if counts["result_bytes"] + result_bytes > max_aggregate_bytes and (
                result.content is not None or result.content_base64 is not None
            ):
                result = BatchURLResult(
                    url=result.url,
                    success=False,
                    format=result.format,
                    duration=result.duration,
                    error=f"Batch aggregate result size exceeded {max_aggregate_bytes} bytes limit",
                    error_type="content_too_large",
                    status_code=413,
                )
                result_bytes = len(result.model_dump_json().encode("utf-8"))
            counts["result_bytes"] += result_bytes
            counts["processed"] += 1
            counts["success" if result.success else "failed"] += 1
            try:
                await job_manager.update_job_status(
                    job_id,
                    JobStatus.RUNNING,
                    progress=int(counts["processed"] * 100 / total_urls),
                    processed_urls=counts["processed"],
                    successful_urls=counts["success"],
                    failed_urls=counts["failed"],
                )
            except (OSError, RuntimeError) as exc:
                logger.warning(f"[JOB-{job_id}] Incremental progress update failed: {exc}")
        return result

    tasks = [_run_item(item, i) for i, item in enumerate(batch_request.urls)]
    results = await asyncio.gather(*tasks)

    batch_duration = time.perf_counter() - started
    successful_count = sum(1 for r in results if r.success)
    failed_count = len(results) - successful_count
    success_rate = (successful_count / len(results)) * 100 if results else 0.0

    if not results:
        await job_manager.update_job_status(
            job_id,
            JobStatus.RUNNING,
            progress=100,
            processed_urls=0,
            successful_urls=0,
            failed_urls=0,
        )

    summary = {
        "total_requests": len(results),
        "successful_requests": successful_count,
        "failed_requests": failed_count,
        "success_rate": success_rate,
        "total_duration": batch_duration,
    }
    return [r.model_dump() for r in results], summary


async def notify_batch_webhook(
    job_id: str,
    batch_request: BatchRequest,
    status: JobStatus,
    *,
    summary: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Deliver a batch completion or failure webhook after job state is persisted."""
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

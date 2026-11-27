"""Batch download job execution engine delegating format conversion to `convert()`."""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from ..config import Settings, get_settings
from ..errors import classify_domain_error, log_classified_error
from ..models.browser_actions import BrowserAction
from ..models.responses import BatchRequest, BatchURLRequest, BatchURLResult, ResponseMetadata
from ..ports import BrowserRenderer, JobStatus, JobStore
from ..validation import (
    canonical_host,
    redact_url_for_log,
    sanitize_custom_headers,
    validate_url_async,
)
from .content_processor import (
    ConversionDeps,
    ConversionRequest,
    ConversionResult,
    HTTPFallbackFetcher,
    OutputFormat,
    convert,
)

if TYPE_CHECKING:
    from ..crawling import DomainThrottle, RobotsChecker

logger = logging.getLogger(__name__)


async def deliver_webhook(*args: Any, **kwargs: Any) -> Any:
    """Delegate outbound batch webhook delivery lazily to the HTTP webhook adapter."""
    from .webhooks import deliver_webhook as _deliver

    return await _deliver(*args, **kwargs)


_BINARY_BATCH_FORMATS = frozenset({"pdf", "screenshot", "raw"})


class _LazyLocalBrowserRenderer:
    """Lazily constructs `LocalBrowserRenderer` only when browser rendering is invoked without DI."""

    def __init__(self, pdf_semaphore: asyncio.Semaphore | None, cfg: Settings) -> None:
        self._pdf_semaphore = pdf_semaphore
        self._cfg = cfg
        self._delegate: BrowserRenderer | None = None

    def _get_delegate(self) -> BrowserRenderer:
        if self._delegate is None:
            from importlib import import_module

            renderer_mod = import_module("downloader.browser.renderer")
            validation_mod = import_module("downloader.validation")
            sem = self._pdf_semaphore or asyncio.Semaphore(self._cfg.pdf.concurrency)
            self._delegate = cast(
                BrowserRenderer,
                renderer_mod.LocalBrowserRenderer(
                    sem,
                    egress_policy=validation_mod.EgressPolicy(self._cfg),
                    max_output_bytes=self._cfg.content.max_download_size,
                ),
            )
        return self._delegate

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        timeout_ms: int | None = None,
        fail_fast: bool = True,
    ) -> bytes | None:
        return await self._get_delegate().render_html(
            url,
            wait_for_selector=wait_for_selector,
            actions=actions,
            timeout_ms=timeout_ms,
            fail_fast=fail_fast,
        )

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None:
        return await self._get_delegate().convert_fallback(url, output_format)

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        timeout_ms: int | None = None,
    ) -> bytes:
        return await self._get_delegate().render_pdf(
            url,
            wait_for_selector=wait_for_selector,
            actions=actions,
            timeout_ms=timeout_ms,
        )

    async def render_screenshot(  # noqa: PLR0913
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        timeout_ms: int | None = None,
    ) -> bytes:
        return await self._get_delegate().render_screenshot(
            url,
            screenshot_format=screenshot_format,
            full_page=full_page,
            wait_for_selector=wait_for_selector,
            actions=actions,
            timeout_ms=timeout_ms,
        )


def _transient_storage_errors() -> tuple[type[BaseException], ...]:
    """Return transient storage error types without importing `redis` into `services/`."""
    errors: list[type[BaseException]] = [OSError, RuntimeError]
    redis_exc = sys.modules.get("redis.exceptions")
    redis_err = getattr(redis_exc, "RedisError", None)
    if isinstance(redis_err, type) and issubclass(redis_err, BaseException):
        errors.append(redis_err)
    return tuple(errors)


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
) -> HTTPFallbackFetcher:
    cached: tuple[bytes, ResponseMetadata] | None = None
    require_text = format_to_use in {"text", "markdown", "html"}
    allow_ocr = require_text

    async def _fetch() -> tuple[bytes, ResponseMetadata]:
        nonlocal cached
        if cached is None:
            cached = await http_client.download(
                validated_url,
                headers=sanitized_headers,
                require_text_convertible=require_text,
                allow_ocr_images=allow_ocr,
                target_format=format_to_use,
            )
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
            if format_to_use in {"pdf", "screenshot"}
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


@dataclass(frozen=True)
class _BatchErrorContext:
    raw_url: str
    format_to_use: str
    request_id: str
    timeout: int
    started: float


def _build_batch_error_result(ctx: _BatchErrorContext, exc: Exception) -> BatchURLResult:
    classified = classify_domain_error(exc, context="batch", batch_timeout=ctx.timeout)
    log_classified_error(logger, f"[{ctx.request_id}] {ctx.raw_url}", exc, classified)
    return BatchURLResult(
        url=ctx.raw_url,
        success=False,
        format=ctx.format_to_use,
        duration=time.perf_counter() - ctx.started,
        error=classified.message,
        error_type=classified.error_type,
        status_code=classified.status_code,
    )


async def _prepare_batch_url(
    url_request: BatchURLRequest,
    format_to_use: str,
    request_id: str,
    exec_deps: BatchExecutionDeps,
    cfg: Settings,
) -> tuple[str, dict[str, str] | None]:
    validated_url = await validate_url_async(url_request.url, cfg)
    sanitized_headers = sanitize_custom_headers(url_request.custom_headers)
    logger.info(
        f"[{request_id}] Processing batch URL: {redact_url_for_log(validated_url)} "
        f"(format: {format_to_use})"
    )
    await _enforce_batch_crawling_policy(validated_url, sanitized_headers, exec_deps, cfg)
    return validated_url, sanitized_headers


async def _convert_prepared_batch_url(
    url_request: BatchURLRequest,
    prepared: tuple[str, dict[str, str] | None],
    ctx: _BatchErrorContext,
    exec_deps: BatchExecutionDeps,
) -> BatchURLResult:
    validated_url, sanitized_headers = prepared
    conv_fmt: OutputFormat = (
        cast(OutputFormat, ctx.format_to_use)
        if ctx.format_to_use in {"text", "markdown", "html", "pdf", "screenshot", "json"}
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
        renderer=exec_deps.renderer,
        http_fetcher=_make_batch_http_fetcher(
            exec_deps.http_client, validated_url, sanitized_headers, ctx.format_to_use
        ),
    )
    conv_res = await asyncio.wait_for(convert(conv_req, conv_deps), timeout=ctx.timeout)
    return _build_batch_success_result(
        url_request.url,
        ctx.format_to_use,
        time.perf_counter() - ctx.started,
        conv_res,
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
    err_ctx = _BatchErrorContext(url_request.url, format_to_use, request_id, timeout, started)
    exec_deps = BatchExecutionDeps(
        http_client=http_client,
        renderer=renderer
        if renderer is not None
        else _LazyLocalBrowserRenderer(pdf_semaphore, cfg),
        robots_checker=robots_checker,
        domain_throttle=domain_throttle,
        settings=cfg,
    )

    try:
        prepared = await _prepare_batch_url(url_request, format_to_use, request_id, exec_deps, cfg)
        return await _convert_prepared_batch_url(
            url_request,
            prepared,
            err_ctx,
            exec_deps,
        )
    except Exception as exc:  # noqa: BLE001
        # Isolate per-URL failures so one failing target never aborts sibling URLs in the batch.
        return _build_batch_error_result(err_ctx, exc)


async def _ensure_job_not_cancelled(job_manager: JobStore, job_id: str) -> None:
    current_job = await job_manager.get_job_info(job_id)
    if current_job is not None and current_job.status == JobStatus.CANCELLED:
        raise asyncio.CancelledError(f"Job {job_id} was cancelled")


async def _record_batch_item_progress(
    job_id: str,
    result: BatchURLResult,
    *,
    counts: dict[str, int],
    job_manager: JobStore,
) -> BatchURLResult:
    max_aggregate_bytes = counts["max_bytes"]
    total_urls = counts["total_urls"]
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
    except _transient_storage_errors() as exc:
        logger.warning(f"[JOB-{job_id}] Incremental progress update failed: {exc}")
    return result


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
    exec_deps = BatchExecutionDeps(
        http_client=http_client,
        renderer=renderer
        if renderer is not None
        else _LazyLocalBrowserRenderer(pdf_semaphore, cfg),
        robots_checker=robots_checker,
        domain_throttle=domain_throttle,
        settings=cfg,
    )
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
    counts = {
        "processed": 0,
        "success": 0,
        "failed": 0,
        "result_bytes": 0,
        "total_urls": total_urls,
        "max_bytes": max_aggregate_bytes,
    }

    async def _run_item(url_request: BatchURLRequest, index: int) -> BatchURLResult:
        request_id = f"JOB-{job_id}-{index + 1:02d}"
        format_to_use = url_request.format or batch_request.default_format

        async with request_semaphore:
            await _ensure_job_not_cancelled(job_manager, job_id)
            err_ctx = _BatchErrorContext(
                url_request.url, format_to_use, request_id, timeout_per_url, time.perf_counter()
            )
            result: BatchURLResult | None = None
            prepared: tuple[str, dict[str, str] | None] | None = None
            try:
                # Run validation and crawl-delay sleeps outside global batch_semaphore.
                prepared = await _prepare_batch_url(
                    url_request, format_to_use, request_id, exec_deps, cfg
                )
            except Exception as exc:  # noqa: BLE001
                result = _build_batch_error_result(err_ctx, exc)

            if result is None and prepared is not None:
                await _ensure_job_not_cancelled(job_manager, job_id)
                async with batch_semaphore:
                    await _ensure_job_not_cancelled(job_manager, job_id)
                    try:
                        result = await _convert_prepared_batch_url(
                            url_request,
                            prepared,
                            err_ctx,
                            exec_deps,
                        )
                    except Exception as exc:  # noqa: BLE001
                        result = _build_batch_error_result(err_ctx, exc)

        assert result is not None
        async with progress_lock:
            return await _record_batch_item_progress(
                job_id,
                result,
                counts=counts,
                job_manager=job_manager,
            )

    results = await asyncio.gather(
        *(_run_item(item, i) for i, item in enumerate(batch_request.urls))
    )
    successful_count = sum(1 for r in results if r.success)
    failed_count = len(results) - successful_count

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
        "success_rate": (successful_count / len(results)) * 100 if results else 0.0,
        "total_duration": time.perf_counter() - started,
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

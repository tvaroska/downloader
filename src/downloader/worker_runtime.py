"""Runtime initialization, teardown, metrics server, and browser error serialization for the split worker."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import redis.exceptions as redis_exc
from pydantic import ValidationError

from .browser.renderer import LocalBrowserRenderer
from .config import Settings
from .crawling import DomainThrottle, RobotsChecker
from .http_client import HTTPClient
from .job_manager import JobManager
from .metrics import get_metrics_collector
from .models.browser_actions import BrowserActionError
from .pdf_generator import (
    PlaywrightPDFGenerator,
    cleanup_pdf_generator,
    set_shared_pdf_generator,
)
from .ports import (
    BatchQueueEnvelope,
    BrowserCapacityError,
    BrowserPoolError,
    BrowserRenderer,
    BrowserTaskRequest,
    BrowserTaskResponse,
    ContentConversionError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    JobStore,
    PDFGeneratorError,
    RenderTimeoutError,
    RobotsDisallowedError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
    WorkQueue,
)
from .scheduler import ExecutionStorage, ScheduledJobExecutor, SchedulerService
from .telemetry import shutdown_telemetry
from .transformers.ocr import shutdown_ocr_executor
from .validation import EgressPolicy, SSRFProtectionError, URLValidationError, canonical_host
from .worker_queue import WorkerQueue

logger = logging.getLogger(__name__)


@dataclass
class _WorkerRuntime:
    worker_id: str
    settings: Settings
    http_client: Any
    pdf_generator: Any
    pdf_semaphore: asyncio.Semaphore
    batch_semaphore: asyncio.Semaphore
    job_slot_semaphore: asyncio.Semaphore
    browser_slot_semaphore: asyncio.Semaphore
    job_manager: JobStore
    worker_queue: WorkQueue
    robots_checker: Any
    domain_throttle: Any
    scheduler: Any
    renderer: BrowserRenderer
    metrics_server: asyncio.Server | None = None


_SIMPLE_BROWSER_TASK_ERRORS: tuple[
    tuple[type[Exception] | tuple[type[Exception], ...], str, int], ...
] = (
    (SSRFProtectionError, "ssrf_protection_error", 400),
    (URLValidationError, "validation_error", 400),
    (RobotsDisallowedError, "robots_disallowed", 403),
    ((BrowserActionError, ValidationError), "browser_action_error", 400),
    (ContentTooLargeError, "content_too_large", 413),
    (ContentConversionError, "conversion_error", 500),
    ((BrowserCapacityError, BrowserPoolError), "service_unavailable", 503),
)


def _error_response(
    task: BrowserTaskRequest,
    error: str,
    error_type: str,
    status_code: int,
    *,
    timeout_ms: int | None = None,
) -> BrowserTaskResponse:
    return BrowserTaskResponse(
        task_id=task.task_id,
        success=False,
        error=error,
        error_type=error_type,
        status_code=status_code,
        timeout_ms=timeout_ms,
    )


def _serialize_browser_task_error(task: BrowserTaskRequest, exc: Exception) -> BrowserTaskResponse:
    """Serialize domain exceptions into `BrowserTaskResponse` with full local/remote parity."""
    for exc_types, error_type, status_code in _SIMPLE_BROWSER_TASK_ERRORS:
        if isinstance(exc, exc_types):
            return _error_response(task, str(exc), error_type, status_code)
    if isinstance(exc, CrawlDelayExceededError):
        return _error_response(
            task,
            str(exc),
            "crawl_delay_exceeded",
            429,
            timeout_ms=max(1, int(exc.retry_after * 1000)),
        )
    if isinstance(exc, SelectorTimeoutError):
        return _error_response(
            task, str(exc), "selector_timeout_error", 408, timeout_ms=exc.timeout_ms
        )
    if isinstance(exc, (RenderTimeoutError, TimeoutError)):
        return _error_response(task, str(exc) or "Browser render timed out", "timeout_error", 408)
    if isinstance(exc, PDFGeneratorError):
        if isinstance(exc, ScreenshotGeneratorError):
            err_type = "screenshot_generation_error"
        else:
            err_type = "http_error" if exc.status_code else "pdf_generation_error"
        return _error_response(task, str(exc), err_type, exc.status_code or 500)
    logger.exception(f"[WORKER] Browser task {task.task_id} failed: {exc}")
    return _error_response(task, str(exc), "render_error", 500)


def _expired_browser_response(task: BrowserTaskRequest) -> BrowserTaskResponse:
    return BrowserTaskResponse(
        task_id=task.task_id,
        success=False,
        error="Browser task deadline expired before execution",
        error_type="timeout_error",
        status_code=408,
    )


async def _start_worker_metrics_server(port: int) -> asyncio.Server:
    """Start a lightweight HTTP endpoint exporting Prometheus metrics for the worker process."""

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            parts = request_line.decode("latin-1", errors="replace").split()
            path = parts[1] if len(parts) >= 2 else "/"
            if path == "/metrics":
                body = get_metrics_collector().get_prometheus_metrics().encode("utf-8")
                header = (
                    "HTTP/1.1 200 OK\r\n"
                    "Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                writer.write(header + body)
            else:
                writer.write(
                    b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            await writer.drain()
        except (TimeoutError, OSError, ValueError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    return await asyncio.start_server(_handle, host="0.0.0.0", port=port)


async def _init_worker_runtime(
    settings: Settings,
    worker_id: str,
    *,
    on_dead_letter: Callable[[BatchQueueEnvelope, JobStore, Settings], Awaitable[None]],
    register_executor: Callable[[ScheduledJobExecutor], None] | None = None,
) -> _WorkerRuntime:
    assert settings.redis.redis_uri is not None
    egress_policy = EgressPolicy(settings)

    job_manager = JobManager(
        settings.redis.redis_uri,
        max_connections=settings.redis.max_connections,
    )
    await job_manager.connect()
    if job_manager.redis_client is None:
        raise RuntimeError("JobManager connected without initializing redis_client")

    worker_queue = WorkerQueue(
        job_manager.redis_client,
        worker_id=worker_id,
        max_remote_browser_calls=settings.worker.max_remote_browser_calls,
        max_attempts=settings.worker.max_attempts,
    )
    await worker_queue.requeue_processing_jobs(
        max_attempts=settings.worker.max_attempts,
        on_dead_letter=lambda env: on_dead_letter(env, job_manager, settings),
        include_self=True,
    )

    robots_checker = RobotsChecker(
        user_agent=settings.crawling.user_agent,
        robots_cache_ttl=settings.crawling.robots_cache_ttl,
        robots_cache_max_size=settings.crawling.robots_cache_max_size,
        robots_fetch_timeout=settings.crawling.robots_fetch_timeout,
        max_crawl_delay=settings.crawling.max_crawl_delay,
        redis_client=job_manager.redis_client,
        egress_policy=egress_policy,
    )
    domain_throttle = DomainThrottle(
        max_crawl_delay=settings.crawling.max_crawl_delay,
        max_size=settings.crawling.robots_cache_max_size,
        redis_client=job_manager.redis_client,
    )

    async def _redirect_policy(_source_url: str, next_url: str) -> None:
        if not settings.crawling.respect_robots_txt:
            return
        delay = await robots_checker.check_allowed(next_url, settings.crawling.user_agent)
        await domain_throttle.wait_if_needed(canonical_host(next_url), delay)

    http_client = HTTPClient(
        timeout=settings.http.request_timeout,
        max_redirects=settings.http.max_redirects,
        user_agent=settings.crawling.user_agent,
        max_download_size=settings.content.max_download_size,
        max_connections=settings.http.max_connections,
        max_keepalive_connections=settings.http.max_keepalive_connections,
        keepalive_expiry=settings.http.keepalive_expiry,
        http2=settings.http.http2_enabled,
        egress_policy=egress_policy,
        redirect_policy=_redirect_policy,
    )

    pdf_generator = PlaywrightPDFGenerator(
        pool_size=settings.pdf.pool_size,
        page_load_timeout=settings.pdf.page_load_timeout,
        wait_until=settings.pdf.wait_until,
        memory_limit_mb=settings.pdf.browser_memory_limit_mb,
        max_uses_per_browser=settings.pdf.max_uses_per_browser,
        idle_timeout=settings.pdf.idle_timeout,
        user_agent=None,
        egress_policy=egress_policy,
        redirect_policy=_redirect_policy,
    )
    set_shared_pdf_generator(pdf_generator)

    pdf_semaphore = asyncio.Semaphore(settings.pdf.concurrency)
    batch_semaphore = asyncio.Semaphore(settings.batch.concurrency)
    job_slot_semaphore = asyncio.Semaphore(settings.worker.max_concurrent_jobs)
    browser_slot_semaphore = asyncio.Semaphore(settings.pdf.concurrency)

    renderer = LocalBrowserRenderer(
        pdf_semaphore,
        egress_policy=egress_policy,
        redirect_policy=_redirect_policy,
        max_output_bytes=settings.content.max_download_size,
    )

    scheduler = SchedulerService(redis_uri=settings.redis.redis_uri, settings=settings.scheduler)
    await scheduler.start(paused=False)
    execution_storage = ExecutionStorage(job_manager.redis_client)
    executor = ScheduledJobExecutor(
        http_client=http_client,
        storage=execution_storage,
        pdf_generator=pdf_generator,
        pdf_semaphore=pdf_semaphore,
        robots_checker=robots_checker,
        domain_throttle=domain_throttle,
        max_workers=settings.scheduler.max_workers,
        renderer=renderer,
        settings=settings,
    )
    if register_executor is not None:
        register_executor(executor)

    metrics_server: asyncio.Server | None = None
    if settings.worker.metrics_port is not None:
        metrics_server = await _start_worker_metrics_server(settings.worker.metrics_port)
        logger.info(f"Worker Prometheus metrics listening on port {settings.worker.metrics_port}")

    return _WorkerRuntime(
        worker_id=worker_id,
        settings=settings,
        http_client=http_client,
        pdf_generator=pdf_generator,
        pdf_semaphore=pdf_semaphore,
        batch_semaphore=batch_semaphore,
        job_slot_semaphore=job_slot_semaphore,
        browser_slot_semaphore=browser_slot_semaphore,
        job_manager=job_manager,
        worker_queue=worker_queue,
        robots_checker=robots_checker,
        domain_throttle=domain_throttle,
        scheduler=scheduler,
        renderer=renderer,
        metrics_server=metrics_server,
    )


async def _drain_active_tasks(
    active_tasks: set[asyncio.Task[Any]],
    drain_timeout: float,
    log: logging.Logger,
) -> None:
    if not active_tasks:
        return
    log.info(
        f"Draining {len(active_tasks)} in-flight worker task(s) (timeout={drain_timeout:.1f}s)"
    )
    done, pending = await asyncio.wait(list(active_tasks), timeout=drain_timeout)
    for pending_task in pending:
        pending_task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    log.info(f"Worker drain finished: {len(done)} completed, {len(pending)} cancelled")


async def _close_worker_resources(rt: _WorkerRuntime) -> None:
    if rt.metrics_server is not None:
        rt.metrics_server.close()
        await rt.metrics_server.wait_closed()
    if rt.scheduler is not None and hasattr(rt.scheduler, "shutdown"):
        await rt.scheduler.shutdown(wait=True)
    for resource in (rt.robots_checker, rt.http_client, rt.pdf_generator, rt.worker_queue):
        if resource is not None and hasattr(resource, "close"):
            await resource.close()
    await cleanup_pdf_generator()
    if hasattr(rt.job_manager, "disconnect"):
        await rt.job_manager.disconnect()
    shutdown_ocr_executor()
    shutdown_telemetry()


async def _shutdown_worker_runtime(
    rt: _WorkerRuntime,
    active_tasks: set[asyncio.Task[Any]],
    log: logging.Logger,
    *,
    heartbeat_stop: asyncio.Event | None = None,
    heartbeat_task: asyncio.Task[None] | None = None,
) -> None:
    await _drain_active_tasks(active_tasks, rt.settings.shutdown.drain_timeout, log)

    # Stop heartbeat only AFTER in-flight jobs finish draining so peers never reclaim live work.
    if heartbeat_stop is not None:
        heartbeat_stop.set()
    if heartbeat_task is not None:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    try:
        await rt.worker_queue.remove_heartbeat(rt.worker_id)
    except (redis_exc.RedisError, OSError, RuntimeError) as exc:
        log.debug(f"Could not remove worker heartbeat during shutdown: {exc}")

    await _close_worker_resources(rt)
    log.info(f"Downloader worker {rt.worker_id} shut down cleanly")

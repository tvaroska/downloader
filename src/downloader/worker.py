"""Dedicated split-worker entrypoint for batch processing, scheduled jobs, and remote Playwright.

Run as:
    python -m src.downloader.worker
Healthcheck:
    python -m src.downloader.worker --healthcheck
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import signal
import socket
import sys
import uuid
from typing import Any

from . import __version__
from .browser.actions import BrowserAction, BrowserActionError
from .config import get_settings
from .content_converter import SelectorTimeoutError, render_html_with_playwright
from .crawling import DomainThrottle, RobotsChecker
from .http_client import HTTPClient
from .job_manager import JobManager, JobStatus
from .logging_config import get_logger, setup_logging
from .models.responses import BatchRequest
from .pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
    cleanup_pdf_generator,
    generate_pdf_from_url,
    generate_screenshot_from_url,
    set_shared_pdf_generator,
)
from .routes.schedules import set_executor as set_schedules_executor
from .scheduler import ExecutionStorage, ScheduledJobExecutor, SchedulerService
from .services.batch_processor import notify_batch_webhook, process_background_batch_job
from .telemetry import (
    configure_telemetry,
    extract_trace_context,
    get_tracer,
    shutdown_telemetry,
)
from .worker_queue import (
    BatchQueueEnvelope,
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerQueue,
)

logger = logging.getLogger(__name__)


async def process_worker_batch_envelope(
    envelope: BatchQueueEnvelope,
    *,
    job_manager: JobManager,
    worker_queue: WorkerQueue,
    http_client: HTTPClient,
    batch_semaphore: asyncio.Semaphore,
    pdf_semaphore: asyncio.Semaphore,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
) -> None:
    """Execute one dequeued batch job envelope, persist results, fire webhooks, and ack."""
    tracer = get_tracer(__name__)
    parent_ctx = extract_trace_context(envelope.trace_headers)
    batch_req: BatchRequest | None = None

    with tracer.start_as_current_span("worker.batch_job", context=parent_ctx) as span:
        span.set_attribute("batch.job_id", envelope.job_id)
        try:
            job_info = await job_manager.get_job_info(envelope.job_id)
            if job_info is None:
                logger.warning(f"[WORKER] Job {envelope.job_id} expired or missing; skipping")
                return
            if job_info.status == JobStatus.CANCELLED:
                logger.info(f"[WORKER] Job {envelope.job_id} already cancelled; skipping")
                return

            batch_req = BatchRequest.model_validate(job_info.request_data)
            await job_manager.update_job_status(envelope.job_id, JobStatus.RUNNING)

            results, summary = await process_background_batch_job(
                envelope.job_id,
                batch_req,
                job_manager,
                batch_semaphore,
                http_client,
                pdf_semaphore,
                robots_checker=robots_checker,
                domain_throttle=domain_throttle,
            )

            await job_manager.store_job_results(envelope.job_id, results, summary)
            await job_manager.update_job_status(envelope.job_id, JobStatus.COMPLETED)
            await notify_batch_webhook(
                envelope.job_id, batch_req, JobStatus.COMPLETED, summary=summary
            )
            logger.info(f"[WORKER] Completed batch job {envelope.job_id}")
        except asyncio.CancelledError:
            await job_manager.update_job_status(envelope.job_id, JobStatus.CANCELLED)
            raise
        except Exception as exc:
            logger.exception(f"[WORKER] Batch job {envelope.job_id} failed: {exc}")
            await job_manager.update_job_status(
                envelope.job_id, JobStatus.FAILED, error_message=str(exc)
            )
            if batch_req is not None:
                await notify_batch_webhook(
                    envelope.job_id, batch_req, JobStatus.FAILED, error=str(exc)
                )
        finally:
            await worker_queue.ack_batch_job(envelope.raw_payload)


async def process_worker_browser_task(
    task: BrowserTaskRequest,
    *,
    worker_queue: WorkerQueue,
    pdf_semaphore: asyncio.Semaphore,
) -> BrowserTaskResponse:
    """Execute one remote Playwright task and publish its serialized result to Redis."""
    tracer = get_tracer(__name__)
    parent_ctx = extract_trace_context(task.trace_headers)
    actions = [BrowserAction.model_validate(item) for item in task.actions]

    with tracer.start_as_current_span("worker.browser_task", context=parent_ctx) as span:
        span.set_attribute("browser.task_id", task.task_id)
        span.set_attribute("browser.task_kind", task.kind)
        try:
            async with pdf_semaphore:
                if task.kind == "pdf":
                    payload_bytes = await generate_pdf_from_url(
                        task.url,
                        actions=actions or None,
                        wait_for_selector=task.wait_for_selector,
                    )
                elif task.kind == "screenshot":
                    payload_bytes = await generate_screenshot_from_url(
                        task.url,
                        {"type": task.screenshot_format, "full_page": task.full_page},
                        actions=actions or None,
                        wait_for_selector=task.wait_for_selector,
                    )
                else:
                    payload_bytes = await render_html_with_playwright(
                        task.url,
                        task.wait_for_selector,
                        actions=actions or None,
                    )

            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=True,
                content_base64=base64.b64encode(payload_bytes).decode("utf-8"),
                status_code=200,
            )
        except SelectorTimeoutError as exc:
            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=False,
                error=str(exc),
                error_type="selector_timeout_error",
                status_code=408,
            )
        except BrowserActionError as exc:
            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=False,
                error=str(exc),
                error_type="browser_action_error",
                status_code=400,
            )
        except PDFGeneratorError as exc:
            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=False,
                error=str(exc),
                error_type="pdf_generation_error",
                status_code=exc.status_code,
            )
        except Exception as exc:
            logger.exception(f"[WORKER] Browser task {task.task_id} failed: {exc}")
            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=False,
                error=str(exc),
                error_type="render_error",
                status_code=500,
            )

        await worker_queue.publish_browser_result(response)
        return response


async def run_healthcheck() -> int:
    """Verify Redis connectivity and at least one live worker heartbeat."""
    settings = get_settings()
    if not settings.redis.redis_uri:
        print("ERROR: REDIS_URI is required for worker healthcheck", file=sys.stderr)
        return 1

    jm = JobManager(settings.redis.redis_uri, max_connections=2)
    try:
        await jm.connect()
        if jm.redis_client is None:
            return 1
        queue = WorkerQueue(jm.redis_client)
        workers = await queue.list_active_workers()
        if not workers:
            print("UNHEALTHY: no active worker heartbeats in Redis", file=sys.stderr)
            return 1
        return 0
    except Exception as exc:
        print(f"UNHEALTHY: {exc}", file=sys.stderr)
        return 1
    finally:
        await jm.disconnect()


async def run_worker() -> None:
    """Run the split worker service until SIGTERM/SIGINT and drain cleanly."""
    settings = get_settings()
    setup_logging(settings.logging)
    log = get_logger(__name__)

    if not settings.redis.redis_uri:
        log.error("REDIS_URI is required to start the split worker process")
        raise SystemExit(1)

    configure_telemetry(settings.telemetry, service_version=__version__)
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    log.info(f"Starting downloader worker {worker_id} (v{__version__})")

    http_client = HTTPClient(
        timeout=settings.http.request_timeout,
        max_redirects=settings.http.max_redirects,
        user_agent=settings.crawling.user_agent,
        max_concurrent=settings.http.max_concurrent_api,
        max_concurrent_batch=settings.http.max_concurrent_batch,
        max_download_size=settings.content.max_download_size,
        max_connections=settings.http.max_connections,
        max_keepalive_connections=settings.http.max_keepalive_connections,
        keepalive_expiry=settings.http.keepalive_expiry,
        http2=settings.http.http2_enabled,
    )

    pdf_generator = PlaywrightPDFGenerator(
        pool_size=settings.pdf.pool_size,
        page_load_timeout=settings.pdf.page_load_timeout,
        wait_until=settings.pdf.wait_until,
        memory_limit_mb=settings.pdf.browser_memory_limit_mb,
        max_uses_per_browser=settings.pdf.max_uses_per_browser,
        idle_timeout=settings.pdf.idle_timeout,
        user_agent=None,
    )
    set_shared_pdf_generator(pdf_generator)

    pdf_semaphore = asyncio.Semaphore(settings.pdf.concurrency)
    batch_semaphore = asyncio.Semaphore(settings.batch.concurrency)
    job_slot_semaphore = asyncio.Semaphore(settings.worker.max_concurrent_jobs)

    job_manager = JobManager(
        settings.redis.redis_uri,
        max_connections=settings.redis.max_connections,
    )
    await job_manager.connect()
    if job_manager.redis_client is None:
        raise RuntimeError("JobManager connected without initializing redis_client")

    worker_queue = WorkerQueue(job_manager.redis_client)
    await worker_queue.requeue_processing_jobs()

    robots_checker = RobotsChecker(
        user_agent=settings.crawling.user_agent,
        robots_cache_ttl=settings.crawling.robots_cache_ttl,
        robots_cache_max_size=settings.crawling.robots_cache_max_size,
        robots_fetch_timeout=settings.crawling.robots_fetch_timeout,
        max_crawl_delay=settings.crawling.max_crawl_delay,
        redis_client=job_manager.redis_client,
    )
    domain_throttle = DomainThrottle(
        max_crawl_delay=settings.crawling.max_crawl_delay,
        max_size=settings.crawling.robots_cache_max_size,
        redis_client=job_manager.redis_client,
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
    )
    scheduler.set_executor(executor)
    set_schedules_executor(executor)

    stop_event = asyncio.Event()
    active_tasks: set[asyncio.Task[Any]] = set()
    loop = asyncio.get_running_loop()

    def _signal_handler(sig: signal.Signals) -> None:
        log.info(f"Worker {worker_id} received {sig.name}; entering graceful drain")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            pass

    def _track(task: asyncio.Task[Any]) -> None:
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)

    async def _heartbeat_loop() -> None:
        while not stop_event.is_set():
            try:
                await worker_queue.publish_heartbeat(
                    worker_id,
                    {
                        "hostname": socket.gethostname(),
                        "version": __version__,
                        "active_tasks": len(active_tasks),
                        "remote_browser_enabled": settings.worker.remote_browser_enabled,
                    },
                    ttl=settings.worker.heartbeat_ttl,
                )
            except Exception as exc:
                log.warning(f"Worker heartbeat publish failed: {exc}")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=settings.worker.heartbeat_interval
                )
            except TimeoutError:
                continue

    async def _batch_consumer_loop() -> None:
        while not stop_event.is_set():
            await job_slot_semaphore.acquire()
            if stop_event.is_set():
                job_slot_semaphore.release()
                break
            try:
                envelope = await worker_queue.pop_batch_job(timeout=settings.worker.poll_timeout)
            except Exception as exc:
                job_slot_semaphore.release()
                log.warning(f"Error polling batch queue: {exc}")
                await asyncio.sleep(1.0)
                continue

            if envelope is None:
                job_slot_semaphore.release()
                continue

            current_envelope = envelope

            async def _run_batch(env: BatchQueueEnvelope = current_envelope) -> None:
                try:
                    await process_worker_batch_envelope(
                        env,
                        job_manager=job_manager,
                        worker_queue=worker_queue,
                        http_client=http_client,
                        batch_semaphore=batch_semaphore,
                        pdf_semaphore=pdf_semaphore,
                        robots_checker=robots_checker,
                        domain_throttle=domain_throttle,
                    )
                finally:
                    job_slot_semaphore.release()

            _track(asyncio.create_task(_run_batch()))

    async def _browser_consumer_loop() -> None:
        while not stop_event.is_set():
            try:
                task = await worker_queue.pop_browser_task(timeout=settings.worker.poll_timeout)
            except Exception as exc:
                log.warning(f"Error polling browser queue: {exc}")
                await asyncio.sleep(1.0)
                continue
            if task is None:
                continue

            current_task = task

            async def _run_browser(req: BrowserTaskRequest = current_task) -> None:
                await process_worker_browser_task(
                    req,
                    worker_queue=worker_queue,
                    pdf_semaphore=pdf_semaphore,
                )

            _track(asyncio.create_task(_run_browser()))

    loops = [
        asyncio.create_task(_heartbeat_loop()),
        asyncio.create_task(_batch_consumer_loop()),
    ]
    if settings.worker.remote_browser_enabled:
        loops.append(asyncio.create_task(_browser_consumer_loop()))

    await stop_event.wait()
    for loop_task in loops:
        loop_task.cancel()
    await asyncio.gather(*loops, return_exceptions=True)

    if active_tasks:
        log.info(
            f"Draining {len(active_tasks)} in-flight worker task(s) "
            f"(timeout={settings.shutdown.drain_timeout:.1f}s)"
        )
        done, pending = await asyncio.wait(
            list(active_tasks), timeout=settings.shutdown.drain_timeout
        )
        for pending_task in pending:
            pending_task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        log.info(f"Worker drain finished: {len(done)} completed, {len(pending)} cancelled")

    try:
        await worker_queue.remove_heartbeat(worker_id)
    except Exception:
        pass

    await scheduler.shutdown(wait=True)
    await robots_checker.close()
    await http_client.close()
    await pdf_generator.close()
    await cleanup_pdf_generator()
    await job_manager.disconnect()
    shutdown_telemetry()
    log.info(f"Downloader worker {worker_id} shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="REST API Downloader split worker process")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="Verify Redis connectivity and an active worker heartbeat, then exit",
    )
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(asyncio.run(run_healthcheck()))
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()

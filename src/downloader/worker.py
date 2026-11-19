"""Dedicated split-worker entrypoint for batch processing, scheduled jobs, and remote Playwright.

Run as:
    python -m downloader.worker
Healthcheck:
    python -m downloader.worker --healthcheck
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import logging
import os
import signal
import socket
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis.exceptions as redis_exc
from pydantic import ValidationError

from . import __version__
from .browser.renderer import LocalBrowserRenderer, render_html_with_playwright
from .config import Settings, get_settings
from .crawling import DomainThrottle, RobotsChecker
from .job_manager import JobManager
from .logging_config import setup_logging
from .metrics import get_metrics_collector
from .models.browser_actions import BrowserAction
from .models.responses import BatchRequest
from .pdf_generator import generate_pdf_from_url, generate_screenshot_from_url
from .ports import (
    TERMINAL_JOB_STATUSES,
    BatchQueueEnvelope,
    BrowserRenderer,
    BrowserTaskRequest,
    BrowserTaskResponse,
    JobStatus,
    JobStore,
    WorkQueue,
)
from .routes.schedules import set_executor as set_schedules_executor
from .services.batch_processor import notify_batch_webhook, process_background_batch_job
from .services.webhooks import open_webhook_secret
from .telemetry import configure_telemetry, extract_trace_context, get_tracer
from .validation import EgressPolicy
from .worker_queue import WorkerQueue
from .worker_runtime import (
    _expired_browser_response,
    _init_worker_runtime,
    _serialize_browser_task_error,
    _shutdown_worker_runtime,
    _start_worker_metrics_server,
    _WorkerRuntime,
)

__all__ = [
    "_WorkerRuntime",
    "_expired_browser_response",
    "_serialize_browser_task_error",
    "_shutdown_worker_runtime",
    "_start_worker_metrics_server",
    "main",
    "process_worker_batch_envelope",
    "process_worker_browser_task",
    "run_healthcheck",
    "run_worker",
]

logger = logging.getLogger(__name__)


def _decode_batch_request(request_data: dict[str, Any], cfg: Settings) -> BatchRequest:
    """Rehydrate a persisted batch request, decrypting `webhook_secret_enc` if present."""
    payload = {k: v for k, v in request_data.items() if k != "webhook_secret_enc"}
    sealed = request_data.get("webhook_secret_enc")
    if isinstance(sealed, str) and sealed:
        payload["webhook_secret"] = open_webhook_secret(sealed, cfg)
    elif isinstance(payload.get("webhook_secret"), str) and payload["webhook_secret"]:
        payload["webhook_secret"] = open_webhook_secret(payload["webhook_secret"], cfg)
    return BatchRequest.model_validate(payload)


async def _fail_dead_letter_batch_job(
    envelope: BatchQueueEnvelope,
    *,
    job_manager: JobStore,
    settings: Settings,
) -> None:
    """Mark an exhausted batch job FAILED and fire its failure webhook."""
    msg = f"Batch job exceeded maximum worker attempts ({settings.worker.max_attempts})"
    batch_req: BatchRequest | None = None
    try:
        job_info = await job_manager.get_job_info(envelope.job_id)
        if job_info is not None:
            batch_req = _decode_batch_request(job_info.request_data, settings)
    except (redis_exc.RedisError, ValidationError, ValueError, OSError) as exc:
        logger.warning(f"[WORKER] Could not load dead-letter job {envelope.job_id}: {exc}")

    should_notify = True
    try:
        should_notify = await job_manager.update_job_status(
            envelope.job_id,
            JobStatus.FAILED,
            error_message=msg,
        )
    except (redis_exc.RedisError, OSError, RuntimeError, ValueError) as exc:
        logger.error(f"[WORKER] Failed updating dead-letter job {envelope.job_id}: {exc}")

    get_metrics_collector().increment_counter("worker_batch_jobs_dead_letter_total")
    if should_notify and batch_req is not None:
        await notify_batch_webhook(envelope.job_id, batch_req, JobStatus.FAILED, error=msg)


async def _handle_batch_envelope_failure(
    envelope: BatchQueueEnvelope,
    exc: Exception,
    *,
    job_manager: JobStore,
    batch_req: BatchRequest | None,
) -> None:
    logger.exception(f"[WORKER] Batch job {envelope.job_id} failed: {exc}")
    get_metrics_collector().increment_counter("worker_batch_jobs_failed_total")
    should_notify = True
    try:
        should_notify = await job_manager.update_job_status(
            envelope.job_id, JobStatus.FAILED, error_message=str(exc)
        )
    except (redis_exc.RedisError, OSError, RuntimeError, ValueError) as status_exc:
        logger.error(f"[WORKER] Could not mark batch job {envelope.job_id} FAILED: {status_exc}")
    if should_notify and batch_req is not None:
        await notify_batch_webhook(envelope.job_id, batch_req, JobStatus.FAILED, error=str(exc))


async def _load_runnable_batch_request(
    envelope: BatchQueueEnvelope,
    job_manager: JobStore,
    cfg: Settings,
) -> BatchRequest | None:
    job_info = await job_manager.get_job_info(envelope.job_id)
    if job_info is None:
        logger.warning(f"[WORKER] Job {envelope.job_id} expired or missing; skipping")
        return None
    if job_info.status in TERMINAL_JOB_STATUSES:
        logger.info(
            f"[WORKER] Job {envelope.job_id} already in terminal status "
            f"{job_info.status.value}; skipping"
        )
        return None
    if envelope.attempt > cfg.worker.max_attempts:
        await _fail_dead_letter_batch_job(envelope, job_manager=job_manager, settings=cfg)
        return None
    batch_req = _decode_batch_request(job_info.request_data, cfg)
    started = await job_manager.update_job_status(envelope.job_id, JobStatus.RUNNING)
    if not started:
        logger.info(f"[WORKER] Job {envelope.job_id} transitioned before start; skipping")
        return None
    return batch_req


async def _finalize_batch_envelope(
    envelope: BatchQueueEnvelope,
    batch_req: BatchRequest,
    results: list[dict[str, Any]],
    summary: dict[str, Any],
    job_manager: JobStore,
) -> None:
    metrics = get_metrics_collector()
    duration = float(summary["total_duration"]) if "total_duration" in summary else None
    applied = await job_manager.complete_job(
        envelope.job_id,
        results,
        summary,
        total_duration=duration,
        completed_at=datetime.now(UTC),
    )
    if applied:
        metrics.increment_counter("worker_batch_jobs_completed_total")
        await notify_batch_webhook(envelope.job_id, batch_req, JobStatus.COMPLETED, summary=summary)
        logger.info(f"[WORKER] Completed batch job {envelope.job_id}")
    else:
        metrics.increment_counter("worker_batch_jobs_cancelled_total")
        logger.info(
            f"[WORKER] Skipped completion blob/webhook for job {envelope.job_id} (already terminal)"
        )


async def process_worker_batch_envelope(  # noqa: PLR0913
    envelope: BatchQueueEnvelope,
    *,
    job_manager: JobStore,
    worker_queue: WorkQueue,
    http_client: Any,
    batch_semaphore: asyncio.Semaphore,
    pdf_semaphore: asyncio.Semaphore,
    robots_checker: RobotsChecker | None = None,
    domain_throttle: DomainThrottle | None = None,
    renderer: BrowserRenderer | None = None,
    settings: Settings | None = None,
) -> None:
    """Execute one dequeued batch job envelope, persist results, fire webhooks, and ack."""
    tracer = get_tracer(__name__)
    parent_ctx = extract_trace_context(envelope.trace_headers)
    cfg = settings or get_settings()
    batch_req: BatchRequest | None = None

    with tracer.start_as_current_span("worker.batch_job", context=parent_ctx) as span:
        span.set_attribute("batch.job_id", envelope.job_id)
        span.set_attribute("batch.attempt", envelope.attempt)
        should_ack = True
        try:
            batch_req = await _load_runnable_batch_request(envelope, job_manager, cfg)
            if batch_req is None:
                return
            active_renderer = renderer or LocalBrowserRenderer(
                pdf_semaphore,
                egress_policy=EgressPolicy(cfg),
                max_output_bytes=cfg.content.max_download_size,
            )
            results, summary = await process_background_batch_job(
                envelope.job_id,
                batch_req,
                job_manager,
                batch_semaphore,
                http_client,
                pdf_semaphore,
                robots_checker=robots_checker,
                domain_throttle=domain_throttle,
                renderer=active_renderer,
                settings=cfg,
            )
            await _finalize_batch_envelope(envelope, batch_req, results, summary, job_manager)
        except asyncio.CancelledError:
            current = await job_manager.get_job_info(envelope.job_id)
            if current is None or current.status != JobStatus.CANCELLED:
                if await worker_queue.requeue_batch_job(envelope):
                    should_ack = False
            raise
        except Exception as exc:  # noqa: BLE001
            # Isolate dequeued job failures so the worker marks the job FAILED and stays alive.
            await _handle_batch_envelope_failure(
                envelope, exc, job_manager=job_manager, batch_req=batch_req
            )
        finally:
            if should_ack:
                await worker_queue.ack_batch_job(envelope.raw_payload)


async def _execute_browser_task_payload(
    task: BrowserTaskRequest,
    actions: list[BrowserAction],
    *,
    egress_policy: EgressPolicy | None,
    redirect_policy: Any,
    max_output_bytes: int | None,
) -> bytes:
    if task.kind == "pdf":
        pdf_kwargs: dict[str, Any] = {
            "actions": actions or None,
            "wait_for_selector": task.wait_for_selector,
        }
        if max_output_bytes is not None:
            pdf_kwargs["max_output_bytes"] = max_output_bytes
        return await generate_pdf_from_url(task.url, **pdf_kwargs)
    if task.kind == "screenshot":
        ss_kwargs: dict[str, Any] = {
            "actions": actions or None,
            "wait_for_selector": task.wait_for_selector,
        }
        if max_output_bytes is not None:
            ss_kwargs["max_output_bytes"] = max_output_bytes
        return await generate_screenshot_from_url(
            task.url,
            {"type": task.screenshot_format, "full_page": task.full_page},
            **ss_kwargs,
        )
    html_kwargs: dict[str, Any] = {"actions": actions or None}
    if egress_policy is not None:
        html_kwargs["egress_policy"] = egress_policy
    if redirect_policy is not None:
        html_kwargs["redirect_policy"] = redirect_policy
    if max_output_bytes is not None:
        html_kwargs["max_output_bytes"] = max_output_bytes
    return await render_html_with_playwright(task.url, task.wait_for_selector, **html_kwargs)


async def process_worker_browser_task(  # noqa: PLR0913
    task: BrowserTaskRequest,
    *,
    worker_queue: WorkQueue,
    pdf_semaphore: asyncio.Semaphore,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: Any = None,
    max_output_bytes: int | None = None,
) -> BrowserTaskResponse:
    """Execute one remote Playwright task and publish its serialized result to the `WorkQueue`."""
    tracer = get_tracer(__name__)
    parent_ctx = extract_trace_context(task.trace_headers)
    metrics = get_metrics_collector()

    with tracer.start_as_current_span("worker.browser_task", context=parent_ctx) as span:
        span.set_attribute("browser.task_id", task.task_id)
        span.set_attribute("browser.task_kind", task.kind)
        if task.is_expired():
            metrics.increment_counter("worker_browser_tasks_expired_total")
            expired_resp = _expired_browser_response(task)
            await worker_queue.publish_browser_result(expired_resp)
            return expired_resp

        try:
            actions = [BrowserAction.model_validate(item) for item in task.actions]
            async with pdf_semaphore:
                if task.is_expired():
                    metrics.increment_counter("worker_browser_tasks_expired_total")
                    expired_resp = _expired_browser_response(task)
                    await worker_queue.publish_browser_result(expired_resp)
                    return expired_resp
                payload_bytes = await _execute_browser_task_payload(
                    task,
                    actions,
                    egress_policy=egress_policy,
                    redirect_policy=redirect_policy,
                    max_output_bytes=max_output_bytes,
                )

            metrics.increment_counter("worker_browser_tasks_completed_total")
            response = BrowserTaskResponse(
                task_id=task.task_id,
                success=True,
                content_base64=base64.b64encode(payload_bytes).decode("utf-8"),
                status_code=200,
            )
        except Exception as exc:  # noqa: BLE001
            # Serialize any domain or unexpected Playwright exception into the reply envelope.
            metrics.increment_counter("worker_browser_tasks_failed_total")
            response = _serialize_browser_task_error(task, exc)

        await worker_queue.publish_browser_result(response)
        return response


def _get_worker_id_file() -> Path:
    configured = os.getenv("WORKER_ID_FILE")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "downloader-worker-id"


async def run_healthcheck() -> int:
    """Verify Redis connectivity and this worker process's own live heartbeat."""
    settings = get_settings()
    if not settings.redis.redis_uri:
        print("ERROR: REDIS_URI is required for worker healthcheck", file=sys.stderr)
        return 1

    worker_id = os.getenv("WORKER_ID")
    if not worker_id:
        id_file = _get_worker_id_file()
        if id_file.is_file():
            worker_id = id_file.read_text(encoding="utf-8").strip()
    if not worker_id:
        print("UNHEALTHY: no local worker_id registered", file=sys.stderr)
        return 1

    jm = JobManager(settings.redis.redis_uri, max_connections=2)
    try:
        await jm.connect()
        if jm.redis_client is None:
            return 1
        queue = WorkerQueue(jm.redis_client)
        if not await queue.has_heartbeat(worker_id):
            print(f"UNHEALTHY: no active heartbeat for worker {worker_id}", file=sys.stderr)
            return 1
        return 0
    except (redis_exc.RedisError, OSError, RuntimeError, ValueError) as exc:
        print(f"UNHEALTHY: {exc}", file=sys.stderr)
        return 1
    finally:
        await jm.disconnect()


async def _run_heartbeat_loop(
    rt: _WorkerRuntime,
    heartbeat_stop: asyncio.Event,
    active_tasks: set[asyncio.Task[Any]],
    log: logging.Logger,
) -> None:
    metrics = get_metrics_collector()
    while not heartbeat_stop.is_set():
        try:
            await rt.worker_queue.publish_heartbeat(
                rt.worker_id,
                {
                    "hostname": socket.gethostname(),
                    "version": __version__,
                    "active_tasks": len(active_tasks),
                    "remote_browser_enabled": rt.settings.worker.remote_browser_enabled,
                },
                ttl=rt.settings.worker.heartbeat_ttl,
            )
            metrics.set_gauge("worker_active_tasks", float(len(active_tasks)))
            metrics.set_gauge("worker_last_heartbeat_timestamp", time.time())
            available = getattr(rt.pdf_generator, "available_browsers", None)
            if isinstance(available, int) and not isinstance(available, bool):
                metrics.set_gauge("pdf_pool_available", float(max(0, available)))
        except (redis_exc.RedisError, OSError, RuntimeError) as exc:
            log.warning(f"Worker heartbeat publish failed: {exc}")
        try:
            await asyncio.wait_for(
                heartbeat_stop.wait(), timeout=rt.settings.worker.heartbeat_interval
            )
        except TimeoutError:
            continue


async def _run_reclaim_loop(
    rt: _WorkerRuntime,
    stop_event: asyncio.Event,
    log: logging.Logger,
) -> None:
    """Periodically reclaim orphaned batch jobs from dead workers."""
    interval = max(1.0, float(rt.settings.worker.heartbeat_interval))
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
            break
        except TimeoutError:
            pass
        try:
            moved = await rt.worker_queue.requeue_processing_jobs(
                max_attempts=rt.settings.worker.max_attempts,
                on_dead_letter=lambda env: _fail_dead_letter_batch_job(
                    env, job_manager=rt.job_manager, settings=rt.settings
                ),
            )
            if moved:
                get_metrics_collector().increment_counter(
                    "worker_batch_jobs_reclaimed_total", moved
                )
        except (redis_exc.RedisError, OSError, RuntimeError, TypeError) as exc:
            log.warning(f"Periodic orphaned-job reclaim failed: {exc}")


async def _run_batch_consumer_loop(
    rt: _WorkerRuntime,
    stop_event: asyncio.Event,
    track_fn: Any,
    log: logging.Logger,
) -> None:
    while not stop_event.is_set():
        await rt.job_slot_semaphore.acquire()
        if stop_event.is_set():
            rt.job_slot_semaphore.release()
            break
        try:
            envelope = await rt.worker_queue.pop_batch_job(timeout=rt.settings.worker.poll_timeout)
        except (redis_exc.RedisError, OSError, RuntimeError) as exc:
            rt.job_slot_semaphore.release()
            log.warning(f"Error polling batch queue: {exc}")
            await asyncio.sleep(1.0)
            continue

        if envelope is None:
            rt.job_slot_semaphore.release()
            continue

        captured_env: BatchQueueEnvelope = envelope

        async def _run_batch(env: BatchQueueEnvelope = captured_env) -> None:
            try:
                await process_worker_batch_envelope(
                    env,
                    job_manager=rt.job_manager,
                    worker_queue=rt.worker_queue,
                    http_client=rt.http_client,
                    batch_semaphore=rt.batch_semaphore,
                    pdf_semaphore=rt.pdf_semaphore,
                    robots_checker=rt.robots_checker,
                    domain_throttle=rt.domain_throttle,
                    renderer=rt.renderer,
                    settings=rt.settings,
                )
            finally:
                rt.job_slot_semaphore.release()

        track_fn(asyncio.create_task(_run_batch()))


async def _run_browser_consumer_loop(
    rt: _WorkerRuntime,
    stop_event: asyncio.Event,
    track_fn: Any,
    log: logging.Logger,
) -> None:
    egress_policy = EgressPolicy(rt.settings)
    redirect_policy = getattr(rt.renderer, "redirect_policy", None)
    max_output_bytes = rt.settings.content.max_download_size

    while not stop_event.is_set():
        await rt.browser_slot_semaphore.acquire()
        if stop_event.is_set():
            rt.browser_slot_semaphore.release()
            break
        try:
            task = await rt.worker_queue.pop_browser_task(timeout=rt.settings.worker.poll_timeout)
        except (redis_exc.RedisError, ValidationError, ValueError, OSError, RuntimeError) as exc:
            rt.browser_slot_semaphore.release()
            log.warning(f"Error polling browser queue: {exc}")
            await asyncio.sleep(1.0)
            continue
        if task is None:
            rt.browser_slot_semaphore.release()
            continue

        captured_task: BrowserTaskRequest = task

        async def _run_browser(req: BrowserTaskRequest = captured_task) -> None:
            try:
                await process_worker_browser_task(
                    req,
                    worker_queue=rt.worker_queue,
                    pdf_semaphore=rt.pdf_semaphore,
                    egress_policy=egress_policy,
                    redirect_policy=redirect_policy,
                    max_output_bytes=max_output_bytes,
                )
            finally:
                rt.browser_slot_semaphore.release()

        track_fn(asyncio.create_task(_run_browser()))


def _register_worker_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    worker_id: str,
    stop_event: asyncio.Event,
    log: logging.Logger,
) -> None:
    def _signal_handler(sig: signal.Signals) -> None:
        log.info(f"Worker {worker_id} received {sig.name}; entering graceful drain")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler, sig)


def _cleanup_worker_lifecycle(
    loop: asyncio.AbstractEventLoop, id_file: Path, worker_id: str
) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.remove_signal_handler(sig)
    with contextlib.suppress(OSError):
        if id_file.is_file() and id_file.read_text(encoding="utf-8").strip() == worker_id:
            id_file.unlink()


async def run_worker(
    *,
    settings: Settings | None = None,
    runtime: _WorkerRuntime | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run the split worker service until SIGTERM/SIGINT and drain cleanly."""
    active_settings = settings or get_settings()
    setup_logging(active_settings.logging)
    log = logging.getLogger(__name__)

    if runtime is None and not active_settings.redis.redis_uri:
        log.error("REDIS_URI is required to start the split worker process")
        raise SystemExit(1)

    configure_telemetry(active_settings.telemetry, service_version=__version__)
    worker_id = (
        runtime.worker_id
        if runtime is not None
        else (os.getenv("WORKER_ID") or f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
    )
    id_file = _get_worker_id_file()
    id_file.write_text(worker_id, encoding="utf-8")
    log.info(f"Starting downloader worker {worker_id} (v{__version__})")

    async def _dead_letter_cb(env: BatchQueueEnvelope, jm: JobStore, cfg: Settings) -> None:
        await _fail_dead_letter_batch_job(env, job_manager=jm, settings=cfg)

    rt = (
        runtime
        if runtime is not None
        else await _init_worker_runtime(
            active_settings,
            worker_id,
            on_dead_letter=_dead_letter_cb,
            register_executor=set_schedules_executor,
        )
    )
    active_stop = stop_event or asyncio.Event()
    heartbeat_stop = asyncio.Event()
    active_tasks: set[asyncio.Task[Any]] = set()
    loop = asyncio.get_running_loop()
    _register_worker_signal_handlers(loop, worker_id, active_stop, log)

    def _track(task: asyncio.Task[Any]) -> None:
        active_tasks.add(task)
        task.add_done_callback(active_tasks.discard)

    heartbeat_task = asyncio.create_task(_run_heartbeat_loop(rt, heartbeat_stop, active_tasks, log))
    consumer_loops = [
        asyncio.create_task(_run_batch_consumer_loop(rt, active_stop, _track, log)),
        asyncio.create_task(_run_reclaim_loop(rt, active_stop, log)),
    ]
    if active_settings.worker.remote_browser_enabled:
        consumer_loops.append(
            asyncio.create_task(_run_browser_consumer_loop(rt, active_stop, _track, log))
        )

    try:
        await active_stop.wait()
        for loop_task in consumer_loops:
            loop_task.cancel()
        await asyncio.gather(*consumer_loops, return_exceptions=True)
        await _shutdown_worker_runtime(
            rt,
            active_tasks,
            log,
            heartbeat_stop=heartbeat_stop,
            heartbeat_task=heartbeat_task,
        )
    finally:
        _cleanup_worker_lifecycle(loop, id_file, worker_id)


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

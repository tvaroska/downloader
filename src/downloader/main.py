"""Main FastAPI application module wiring Ports, Adapters and Contracts."""

from __future__ import annotations

import asyncio
import inspect
import logging
import multiprocessing
import time
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, cast

import redis.exceptions as redis_exc
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__
from .api import router
from .auth import get_auth_status
from .config import Settings, get_settings
from .crawling import DomainThrottle, RobotsChecker
from .dependencies import build_browser_renderer
from .drain import DrainCoordinator, DrainMiddleware, install_sigterm_drain_handler
from .http_client import HTTPClient, RedirectPolicyHook
from .job_manager import JobManager
from .logging_config import setup_logging
from .middleware import MetricsMiddleware, get_system_metrics_collector
from .models.responses import ErrorResponse
from .pdf_generator import (
    PDFGeneratorError,
    PlaywrightPDFGenerator,
    cleanup_pdf_generator,
    set_shared_pdf_generator,
)
from .ports import WorkQueue
from .ratelimit_middleware import RateLimitMiddleware, reset_rate_limiter
from .routes.schedules import set_executor as set_schedules_executor
from .scheduler import ExecutionStorage, ScheduledJobExecutor, SchedulerService
from .telemetry import configure_telemetry, shutdown_telemetry
from .transformers.ocr import shutdown_ocr_executor
from .validation import EgressPolicy, canonical_host
from .worker_queue import WorkerQueue

settings = get_settings()
setup_logging(settings.logging)
logger = logging.getLogger(__name__)

_REDIS_HEALTH_PROBE_TTL_SECONDS = 10.0
_JOB_MANAGER_PROBE_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, int, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


def _log_startup_validation(current_settings: Settings) -> None:
    logger.info(
        f"Starting {current_settings.app_name} v{__version__} "
        f"(mode={current_settings.worker.deployment_mode})"
    )
    logger.info(f"Environment: {current_settings.environment}")
    for message in current_settings.validate_settings():
        if message.startswith("WARNING: "):
            logger.warning(message.removeprefix("WARNING: "))
        elif message.startswith("ERROR: "):
            logger.error(message.removeprefix("ERROR: "))
        else:
            logger.info(message.removeprefix("INFO: "))


def _make_redirect_policy(app: FastAPI) -> RedirectPolicyHook:
    """Enforce `robots.txt` and `Crawl-delay` when HTTP or browser clients cross origins."""

    async def _check_cross_origin_redirect(_source_url: str, next_url: str) -> None:
        active_cfg: Settings = getattr(app.state, "settings", None) or get_settings()
        if not active_cfg.crawling.respect_robots_txt:
            return
        checker: RobotsChecker | None = getattr(app.state, "robots_checker", None)
        if checker is None:
            return
        delay = await checker.check_allowed(next_url, active_cfg.crawling.user_agent)
        throttle: DomainThrottle | None = getattr(app.state, "domain_throttle", None)
        if throttle is not None:
            await throttle.wait_if_needed(canonical_host(next_url), delay)

    return _check_cross_origin_redirect


async def _init_redis_and_crawling(
    app: FastAPI,
    current_settings: Settings,
    egress_policy: EgressPolicy,
) -> JobManager | None:
    job_manager: JobManager | None = None
    if current_settings.redis.redis_uri:
        job_manager = JobManager(
            current_settings.redis.redis_uri,
            max_connections=current_settings.redis.max_connections,
        )
        await job_manager.connect()
        app.state.job_manager = job_manager
        app.state.worker_queue = (
            WorkerQueue(
                job_manager.redis_client,
                max_remote_browser_calls=current_settings.worker.max_remote_browser_calls,
                max_attempts=current_settings.worker.max_attempts,
            )
            if job_manager.redis_client
            else None
        )
    else:
        app.state.job_manager = None
        app.state.worker_queue = None

    redis_client = job_manager.redis_client if job_manager is not None else None
    app.state.robots_checker = RobotsChecker(
        user_agent=current_settings.crawling.user_agent,
        robots_cache_ttl=current_settings.crawling.robots_cache_ttl,
        robots_cache_max_size=current_settings.crawling.robots_cache_max_size,
        robots_fetch_timeout=current_settings.crawling.robots_fetch_timeout,
        max_crawl_delay=current_settings.crawling.max_crawl_delay,
        redis_client=redis_client,
        egress_policy=egress_policy,
    )
    app.state.domain_throttle = DomainThrottle(
        max_crawl_delay=current_settings.crawling.max_crawl_delay,
        max_size=current_settings.crawling.robots_cache_max_size,
        redis_client=redis_client,
    )
    return job_manager


def _init_pdf_generator(
    app: FastAPI,
    current_settings: Settings,
    egress_policy: EgressPolicy,
    redirect_policy: RedirectPolicyHook,
) -> None:
    try:
        pdf_generator = PlaywrightPDFGenerator(
            pool_size=current_settings.pdf.pool_size,
            page_load_timeout=current_settings.pdf.page_load_timeout,
            wait_until=current_settings.pdf.wait_until,
            memory_limit_mb=current_settings.pdf.browser_memory_limit_mb,
            max_uses_per_browser=current_settings.pdf.max_uses_per_browser,
            idle_timeout=current_settings.pdf.idle_timeout,
            user_agent=None,
            egress_policy=egress_policy,
            redirect_policy=redirect_policy,
        )
        set_shared_pdf_generator(pdf_generator)
        app.state.pdf_generator = pdf_generator
    except (PDFGeneratorError, RuntimeError, OSError, ValueError) as exc:
        logger.warning(f"PDF generator initialization failed: {exc}")
        set_shared_pdf_generator(None)
        app.state.pdf_generator = None


async def _init_scheduler(
    app: FastAPI,
    current_settings: Settings,
    job_manager: JobManager | None,
    http_client: HTTPClient,
) -> None:
    if not current_settings.redis.redis_uri or job_manager is None:
        app.state.scheduler = None
        app.state.execution_storage = None
        return

    scheduler_service = SchedulerService(
        redis_uri=current_settings.redis.redis_uri,
        settings=current_settings.scheduler,
    )
    await scheduler_service.start(paused=current_settings.worker.deployment_mode == "api")
    app.state.scheduler = scheduler_service

    if job_manager.redis_client is None:
        raise RuntimeError("JobManager connected without initializing redis_client")
    execution_storage = ExecutionStorage(job_manager.redis_client)
    executor = ScheduledJobExecutor(
        http_client=http_client,
        storage=execution_storage,
        pdf_generator=app.state.pdf_generator,
        pdf_semaphore=app.state.pdf_semaphore,
        robots_checker=app.state.robots_checker,
        domain_throttle=app.state.domain_throttle,
        max_workers=current_settings.scheduler.max_workers,
        renderer=app.state.browser_renderer,
        settings=current_settings,
    )
    set_schedules_executor(executor)
    app.state.execution_storage = execution_storage


async def _run_teardown_step(name: str, step: Callable[[], Any]) -> None:
    """Isolate each shutdown step so one failure never skips subsequent cleanup."""
    try:
        result = step()
        if inspect.isawaitable(result):
            await result
    except (redis_exc.RedisError, OSError, RuntimeError, ValueError, TimeoutError) as exc:
        logger.warning(f"Shutdown step '{name}' failed: {exc}")


async def _teardown_services(
    app: FastAPI,
    current_settings: Settings,
    drain_coordinator: DrainCoordinator,
    system_metrics: Any,
    http_client: HTTPClient,
) -> None:
    logger.info("Shutting down services (entering drain mode)...")
    drain_coordinator.start_drain()
    shutdown_start = time.monotonic()
    drain_timeout = current_settings.shutdown.drain_timeout
    try:
        await _run_teardown_step(
            "drain_coordinator.wait_until_drained",
            lambda: drain_coordinator.wait_until_drained(drain_timeout),
        )
        job_manager = getattr(app.state, "job_manager", None)
        if job_manager is not None:
            remaining_drain = max(0.0, drain_timeout - (time.monotonic() - shutdown_start))
            await _run_teardown_step(
                "job_manager.wait_for_active_jobs",
                lambda: job_manager.wait_for_active_jobs(remaining_drain),
            )
        await _run_teardown_step("system_metrics.stop", system_metrics.stop)
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            await _run_teardown_step(
                "scheduler.shutdown",
                lambda: scheduler.shutdown(wait=True),
            )
        worker_queue = getattr(app.state, "worker_queue", None)
        if worker_queue is not None and hasattr(worker_queue, "close"):
            await _run_teardown_step("worker_queue.close", worker_queue.close)
        if job_manager is not None:
            await _run_teardown_step("job_manager.disconnect", job_manager.disconnect)
        robots_checker = getattr(app.state, "robots_checker", None)
        if robots_checker is not None:
            await _run_teardown_step("robots_checker.close", robots_checker.close)
        await _run_teardown_step("http_client.close", http_client.close)
        pdf_generator = getattr(app.state, "pdf_generator", None)
        if pdf_generator is not None:
            await _run_teardown_step("pdf_generator.close", pdf_generator.close)
        await _run_teardown_step("cleanup_pdf_generator", cleanup_pdf_generator)
    finally:
        shutdown_ocr_executor()
        shutdown_telemetry()
        drain_coordinator.reset()
    logger.info("All services shut down successfully")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and tear down application ports and infrastructure adapters."""
    current_settings = get_settings()
    app.state.settings = current_settings
    egress_policy = EgressPolicy(current_settings)
    app.state.egress_policy = egress_policy
    redirect_policy = _make_redirect_policy(app)
    app.state.redirect_policy = redirect_policy

    drain_coordinator: DrainCoordinator = (
        getattr(app.state, "drain_coordinator", None) or DrainCoordinator()
    )
    drain_coordinator.reset()
    app.state.drain_coordinator = drain_coordinator
    restore_sigterm = install_sigterm_drain_handler(
        drain_coordinator,
        pre_stop_delay=current_settings.shutdown.pre_stop_delay,
    )
    try:
        active_rl_uri = (
            current_settings.ratelimit.storage_uri
            or current_settings.redis.redis_uri
            or "memory://"
        )
        if active_rl_uri.startswith("memory://"):
            reset_rate_limiter()
        configure_telemetry(current_settings.telemetry, app=app, service_version=__version__)
        _log_startup_validation(current_settings)

        app.state.pdf_semaphore = asyncio.Semaphore(current_settings.pdf.concurrency)
        app.state.batch_semaphore = asyncio.Semaphore(current_settings.batch.concurrency)

        job_manager = await _init_redis_and_crawling(app, current_settings, egress_policy)
        http_client = HTTPClient(
            timeout=current_settings.http.request_timeout,
            max_redirects=current_settings.http.max_redirects,
            user_agent=current_settings.crawling.user_agent,
            max_download_size=current_settings.content.max_download_size,
            max_connections=current_settings.http.max_connections,
            max_keepalive_connections=current_settings.http.max_keepalive_connections,
            keepalive_expiry=current_settings.http.keepalive_expiry,
            http2=current_settings.http.http2_enabled,
            egress_policy=egress_policy,
            redirect_policy=redirect_policy,
        )
        app.state.http_client = http_client

        _init_pdf_generator(app, current_settings, egress_policy, redirect_policy)

        # Choose local vs remote BrowserRenderer adapter once at startup.
        app.state.browser_renderer = build_browser_renderer(
            current_settings,
            pdf_semaphore=app.state.pdf_semaphore,
            work_queue=app.state.worker_queue,
            redirect_policy=redirect_policy,
        )

        await _init_scheduler(app, current_settings, job_manager, http_client)

        system_metrics = get_system_metrics_collector()
        await system_metrics.start(app_state=app.state)

        try:
            yield
        finally:
            await _teardown_services(
                app, current_settings, drain_coordinator, system_metrics, http_client
            )
    finally:
        restore_sigterm()


app = FastAPI(
    title="REST API Downloader",
    description="High-performance web service for programmatic URL content downloading",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.state.drain_coordinator = DrainCoordinator()

app.add_middleware(RateLimitMiddleware)
app.add_middleware(DrainMiddleware)
app.add_middleware(MetricsMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return the standard ErrorResponse envelope for FastAPI request validation errors."""
    messages: list[str] = []
    is_invalid_cron = False
    for err in exc.errors():
        loc_tuple = tuple(err.get("loc", ()))
        loc = ".".join(str(part) for part in loc_tuple if part != "body")
        msg = str(err.get("msg", "Invalid value")).removeprefix("Value error, ")
        messages.append(f"{loc}: {msg}" if loc else msg)
        if (
            request.url.path == "/schedules"
            and loc_tuple == ("body", "cron_expression")
            and err.get("type") != "missing"
        ):
            is_invalid_cron = True
    summary = "; ".join(messages) if messages else "Request validation failed"
    return JSONResponse(
        status_code=400 if is_invalid_cron else 422,
        content={
            "detail": ErrorResponse(
                error=summary,
                error_type="validation_error",
            ).model_dump()
        },
    )


def _require_non_prod_docs(request: Request) -> Settings:
    active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if active_settings.environment == "production":
        raise HTTPException(status_code=404, detail="Not Found")
    return active_settings


@app.get("/openapi.json", include_in_schema=False)
async def custom_openapi(request: Request) -> JSONResponse:
    _require_non_prod_docs(request)
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui(request: Request) -> HTMLResponse:
    active_settings = _require_non_prod_docs(request)
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{active_settings.app_name} - Swagger UI",
        oauth2_redirect_url="/docs/oauth2-redirect",
    )


@app.get("/docs/oauth2-redirect", include_in_schema=False)
async def custom_swagger_redirect(request: Request) -> HTMLResponse:
    _require_non_prod_docs(request)
    return get_swagger_ui_oauth2_redirect_html()


@app.get("/redoc", include_in_schema=False)
async def custom_redoc(request: Request) -> HTMLResponse:
    active_settings = _require_non_prod_docs(request)
    return get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{active_settings.app_name} - ReDoc",
    )


async def _probe_job_manager_status(job_manager: Any) -> dict[str, Any]:
    status_dict: dict[str, Any] = {"available": job_manager is not None}
    if job_manager is None:
        status_dict["status"] = "disabled"
        status_dict["reason"] = "Redis connection required"
        return status_dict
    redis_client = getattr(job_manager, "redis_client", None)
    if redis_client is None:
        status_dict["status"] = "not_connected"
        return status_dict

    now = time.monotonic()
    client_id = id(redis_client)
    try:
        cached = _JOB_MANAGER_PROBE_CACHE.get(job_manager)
    except TypeError:
        cached = None
    if (
        cached is not None
        and cached[1] == client_id
        and (now - cached[0]) < _REDIS_HEALTH_PROBE_TTL_SECONDS
    ):
        return dict(cached[2])

    try:
        await cast(Awaitable[bool], redis_client.ping())
        status_dict["status"] = "connected"
    except (redis_exc.RedisError, OSError, RuntimeError):
        status_dict["status"] = "error"
    try:
        _JOB_MANAGER_PROBE_CACHE[job_manager] = (now, client_id, dict(status_dict))
    except TypeError:
        pass
    return status_dict


def _probe_workers_status(app_settings: Settings, worker_queue: WorkQueue | None) -> dict[str, Any]:
    return {
        "deployment_mode": app_settings.worker.deployment_mode,
        "remote_browser_enabled": app_settings.worker.remote_browser_enabled,
        "queue_configured": worker_queue is not None,
    }


async def _get_scheduler_status_nonblocking(scheduler: SchedulerService) -> dict[str, Any]:
    """Read scheduler status without running synchronous RedisJobStore I/O on the event loop."""
    cached_getter = getattr(scheduler, "get_cached_status", None)
    if callable(cached_getter):
        maybe_coro = cached_getter()
        if inspect.isawaitable(maybe_coro):
            cached_result = await maybe_coro
            if isinstance(cached_result, dict):
                return cached_result

    inner = getattr(scheduler, "_scheduler", None)
    started = getattr(scheduler, "_started", None)
    if inner is None or not isinstance(started, bool):
        return await scheduler.get_status()
    if not started:
        return {"status": "stopped"}

    jobs = await asyncio.to_thread(scheduler.get_jobs)
    pending_jobs = [job for job in jobs if job.next_run_time is not None]
    next_run: datetime | None = (
        min(job.next_run_time for job in pending_jobs if job.next_run_time)
        if pending_jobs
        else None
    )
    return {
        "status": "running" if inner.running else "paused",
        "job_store_type": scheduler.settings.job_store_type,
        "total_jobs": len(jobs),
        "pending_jobs": len(pending_jobs),
        "next_run_time": next_run.isoformat() if next_run else None,
    }


@app.get("/health", response_model=None)
async def health_check(request: Request) -> dict[str, Any] | Response:
    """Health check endpoint with configuration-aware monitoring and drain status."""
    app_settings: Settings = request.app.state.settings
    pdf_semaphore: asyncio.Semaphore = request.app.state.pdf_semaphore
    batch_semaphore: asyncio.Semaphore = request.app.state.batch_semaphore
    drain_coordinator: DrainCoordinator | None = getattr(
        request.app.state, "drain_coordinator", None
    )
    is_draining = bool(drain_coordinator and drain_coordinator.is_draining)

    pdf_limit = app_settings.pdf.concurrency
    batch_limit = app_settings.batch.concurrency
    job_manager = getattr(request.app.state, "job_manager", None)
    redis_available = job_manager is not None

    job_manager_status = await _probe_job_manager_status(job_manager)
    batch_in_use = batch_limit - batch_semaphore._value
    batch_util = (batch_in_use / batch_limit) * 100 if batch_limit > 0 else 0.0
    batch_info: dict[str, Any] = (
        {
            "available": True,
            "max_concurrent_downloads": batch_limit,
            "current_active_downloads": batch_in_use,
            "available_slots": batch_semaphore._value,
            "utilization_percent": round(batch_util, 1),
        }
        if redis_available
        else {
            "available": False,
            "reason": "Redis connection (REDIS_URI) required",
        }
    )

    pdf_in_use = pdf_limit - pdf_semaphore._value
    pdf_util = (pdf_in_use / pdf_limit) * 100 if pdf_limit > 0 else 0.0
    pdf_available = getattr(request.app.state, "pdf_generator", None) is not None

    scheduler: SchedulerService | None = getattr(request.app.state, "scheduler", None)
    scheduler_status: dict[str, Any] = {"available": scheduler is not None}
    if scheduler is not None:
        scheduler_status.update(await _get_scheduler_status_nonblocking(scheduler))
    else:
        scheduler_status["reason"] = "Redis connection required"

    worker_queue: WorkQueue | None = getattr(request.app.state, "worker_queue", None)
    workers_status = _probe_workers_status(app_settings, worker_queue)

    health_info: dict[str, Any] = {
        "status": "draining" if is_draining else "healthy",
        "draining": is_draining,
        "active_requests": drain_coordinator.active_requests if drain_coordinator else 0,
        "version": __version__,
        "environment": app_settings.environment,
        "deployment_mode": app_settings.worker.deployment_mode,
        "telemetry_enabled": app_settings.telemetry.enabled,
        "configuration": {
            "pdf_concurrency": pdf_limit,
            "batch_concurrency": batch_limit,
            "max_download_size_mb": app_settings.content.max_download_size / 1024 / 1024,
            "cpu_cores": multiprocessing.cpu_count(),
            "respect_robots_txt": app_settings.crawling.respect_robots_txt,
            "user_agent": app_settings.crawling.user_agent,
            "ocr_enabled": app_settings.ocr.enabled,
            "webhooks_enabled": app_settings.webhook.enabled,
        },
        "services": {
            "job_manager": job_manager_status,
            "batch_processing": batch_info,
            "pdf_generation": {
                "available": pdf_available,
                "max_concurrent_pdfs": pdf_limit,
                "current_active_pdfs": pdf_in_use,
                "available_slots": pdf_semaphore._value,
                "utilization_percent": round(pdf_util, 1),
            },
            "scheduler": scheduler_status,
            "workers": workers_status,
        },
    }
    health_info.update(get_auth_status(app_settings))
    if is_draining:
        return JSONResponse(status_code=503, content=health_info)
    return health_info


app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("downloader.main:app", host="0.0.0.0", port=8000, reload=True)

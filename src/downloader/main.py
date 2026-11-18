"""Main FastAPI application module wiring Ports, Adapters and Contracts."""

from __future__ import annotations

import asyncio
import multiprocessing
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, cast

import redis.exceptions as redis_exc
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from . import __version__
from .api import router
from .auth import get_auth_status
from .config import Settings, get_settings
from .crawling import DomainThrottle, RobotsChecker
from .dependencies import build_browser_renderer
from .drain import DrainCoordinator, DrainMiddleware
from .http_client import HTTPClient, RedirectPolicyHook
from .job_manager import JobManager
from .logging_config import get_logger, setup_logging
from .middleware import MetricsMiddleware, get_system_metrics_collector
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
logger = get_logger(__name__)

storage_uri = settings.ratelimit.storage_uri or settings.redis.redis_uri or "memory://"
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=storage_uri,
    default_limits=[],
    headers_enabled=settings.ratelimit.headers_enabled,
)
logger.info(f"Rate limiter initialized with storage: {storage_uri}")


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
    """Enforce `robots.txt` and `Crawl-delay` when `HTTPClient` follows cross-origin redirects."""

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
            WorkerQueue(job_manager.redis_client) if job_manager.redis_client else None
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
    )
    set_schedules_executor(executor)
    app.state.execution_storage = execution_storage


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
    await drain_coordinator.wait_until_drained(drain_timeout)

    if app.state.job_manager:
        remaining_drain = max(0.0, drain_timeout - (time.monotonic() - shutdown_start))
        await app.state.job_manager.wait_for_active_jobs(remaining_drain)

    await system_metrics.stop()
    if app.state.scheduler:
        await app.state.scheduler.shutdown(wait=True)
    if getattr(app.state, "robots_checker", None):
        await app.state.robots_checker.close()
    await http_client.close()
    if app.state.pdf_generator:
        await app.state.pdf_generator.close()
    await cleanup_pdf_generator()
    if app.state.job_manager:
        await app.state.job_manager.disconnect()
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

    drain_coordinator: DrainCoordinator = (
        getattr(app.state, "drain_coordinator", None) or DrainCoordinator()
    )
    drain_coordinator.reset()
    app.state.drain_coordinator = drain_coordinator
    reset_rate_limiter(limiter)
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
        redirect_policy=_make_redirect_policy(app),
    )
    app.state.http_client = http_client

    _init_pdf_generator(app, current_settings, egress_policy)

    # Choose local vs remote BrowserRenderer adapter once at startup.
    app.state.browser_renderer = build_browser_renderer(
        current_settings,
        pdf_semaphore=app.state.pdf_semaphore,
        work_queue=app.state.worker_queue,
    )
    app.state._browser_renderer_mode = (
        current_settings.worker.deployment_mode,
        current_settings.worker.remote_browser_enabled,
        current_settings.worker.remote_browser_timeout,
        current_settings.content.max_download_size,
    )

    await _init_scheduler(app, current_settings, job_manager, http_client)

    system_metrics = get_system_metrics_collector()
    await system_metrics.start(app_state=app.state)

    yield

    await _teardown_services(app, current_settings, drain_coordinator, system_metrics, http_client)


app = FastAPI(
    title="REST API Downloader",
    description="High-performance web service for programmatic URL content downloading",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

app.state.limiter = limiter
app.state.drain_coordinator = DrainCoordinator()

app.add_middleware(RateLimitMiddleware, limiter=limiter)
app.add_middleware(MetricsMiddleware)
app.add_middleware(DrainMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
        status_dict["reason"] = "Redis connection required"
        return status_dict
    redis_client = getattr(job_manager, "redis_client", None)
    if redis_client is None:
        status_dict["status"] = "not_connected"
        return status_dict
    try:
        await cast(Awaitable[bool], redis_client.ping())
        status_dict["status"] = "connected"
    except (redis_exc.RedisError, OSError, RuntimeError) as exc:
        status_dict["status"] = "error"
        status_dict["error"] = str(exc)
    return status_dict


async def _probe_workers_status(
    app_settings: Settings, worker_queue: WorkQueue | None
) -> dict[str, Any]:
    workers_status: dict[str, Any] = {
        "deployment_mode": app_settings.worker.deployment_mode,
        "remote_browser_enabled": app_settings.worker.remote_browser_enabled,
    }
    if worker_queue is not None:
        try:
            active_workers = await worker_queue.list_active_workers()
            queue_depths = await worker_queue.get_queue_depths()
            workers_status.update(
                {
                    "active_workers": len(active_workers),
                    "workers": active_workers,
                    "queues": queue_depths,
                }
            )
        except (redis_exc.RedisError, OSError, RuntimeError) as exc:
            workers_status["error"] = str(exc)
    return workers_status


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
        scheduler_status.update(await scheduler.get_status())
    else:
        scheduler_status["reason"] = "Redis connection required"

    worker_queue: WorkQueue | None = getattr(request.app.state, "worker_queue", None)
    workers_status = await _probe_workers_status(app_settings, worker_queue)

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

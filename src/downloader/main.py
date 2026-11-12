"""Main FastAPI application module."""

import asyncio
import multiprocessing
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.types import ExceptionHandler

from . import __version__
from .api import router
from .auth import get_auth_status
from .config import get_settings
from .crawling import DomainThrottle, RobotsChecker
from .drain import DrainCoordinator, DrainMiddleware
from .http_client import HTTPClient
from .job_manager import JobManager
from .logging_config import get_logger, setup_logging
from .middleware import MetricsMiddleware, get_system_metrics_collector
from .pdf_generator import (
    PlaywrightPDFGenerator,
    cleanup_pdf_generator,
    set_shared_pdf_generator,
)
from .ratelimit_middleware import RateLimitMiddleware, reset_rate_limiter
from .scheduler import SchedulerService
from .telemetry import configure_telemetry, shutdown_telemetry
from .worker_queue import WorkerQueue

# Load settings and configure logging
settings = get_settings()
setup_logging(settings.logging)
logger = get_logger(__name__)

# Initialize rate limiter
# Use Redis if configured (for distributed rate limiting), otherwise in-memory
storage_uri = settings.ratelimit.storage_uri or settings.redis.redis_uri or "memory://"
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=storage_uri,
    default_limits=[],  # Limits applied per-endpoint via middleware
    headers_enabled=settings.ratelimit.headers_enabled,
)
logger.info(f"Rate limiter initialized with storage: {storage_uri}")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan events - initialize and cleanup resources."""
    # Store settings in app state (call get_settings() to support test env reloading)
    current_settings = get_settings()
    app.state.settings = current_settings
    drain_coordinator: DrainCoordinator = (
        getattr(app.state, "drain_coordinator", None) or DrainCoordinator()
    )
    drain_coordinator.reset()
    app.state.drain_coordinator = drain_coordinator
    reset_rate_limiter(limiter)
    configure_telemetry(current_settings.telemetry, app=app, service_version=__version__)

    # Log configuration validation messages
    logger.info(
        f"Starting {current_settings.app_name} v{__version__} "
        f"(mode={current_settings.worker.deployment_mode})"
    )
    logger.info(f"Environment: {current_settings.environment}")
    for message in current_settings.validate_settings():
        if "WARNING" in message:
            logger.warning(message.replace("WARNING: ", ""))
        elif "ERROR" in message:
            logger.error(message.replace("ERROR: ", ""))
        else:
            logger.info(message.replace("INFO: ", ""))

    # Initialize HTTP client
    logger.info("Initializing HTTP client...")
    http_client = HTTPClient(
        timeout=current_settings.http.request_timeout,
        max_redirects=current_settings.http.max_redirects,
        user_agent=current_settings.crawling.user_agent,
        max_concurrent=current_settings.http.max_concurrent_api,
        max_concurrent_batch=current_settings.http.max_concurrent_batch,
        max_download_size=current_settings.content.max_download_size,
        max_connections=current_settings.http.max_connections,
        max_keepalive_connections=current_settings.http.max_keepalive_connections,
        keepalive_expiry=current_settings.http.keepalive_expiry,
        http2=current_settings.http.http2_enabled,
    )
    app.state.http_client = http_client
    logger.info("HTTP client initialized")

    # Initialize PDF generator (browser pool starts lazily on first PDF/JS-render request)
    logger.info("Configuring PDF generator (lazy browser pool)...")
    try:
        pdf_generator = PlaywrightPDFGenerator(
            pool_size=current_settings.pdf.pool_size,
            page_load_timeout=current_settings.pdf.page_load_timeout,
            wait_until=current_settings.pdf.wait_until,
            memory_limit_mb=current_settings.pdf.browser_memory_limit_mb,
            max_uses_per_browser=current_settings.pdf.max_uses_per_browser,
            idle_timeout=current_settings.pdf.idle_timeout,
            user_agent=None,
        )
        set_shared_pdf_generator(pdf_generator)
        app.state.pdf_generator = pdf_generator
        logger.info(
            f"PDF generator configured for lazy startup (pool_size={current_settings.pdf.pool_size}, timeout={current_settings.pdf.page_load_timeout}ms, wait_until={current_settings.pdf.wait_until})"
        )
    except Exception as e:
        logger.warning(
            f"PDF generator initialization failed: {e}. PDF generation will be unavailable."
        )
        set_shared_pdf_generator(None)
        app.state.pdf_generator = None

    # Initialize semaphores for concurrency control
    app.state.pdf_semaphore = asyncio.Semaphore(current_settings.pdf.concurrency)
    app.state.batch_semaphore = asyncio.Semaphore(current_settings.batch.concurrency)
    logger.info(
        f"Concurrency semaphores initialized (PDF={current_settings.pdf.concurrency}, BATCH={current_settings.batch.concurrency})"
    )

    # Initialize job manager first when Redis is configured so crawling + scheduler share its pool
    job_manager: JobManager | None = None
    if current_settings.redis.redis_uri:
        logger.info("Initializing job manager with Redis...")
        job_manager = JobManager(
            current_settings.redis.redis_uri,
            max_connections=current_settings.redis.max_connections,
        )
        await job_manager.connect()
        app.state.job_manager = job_manager
        app.state.worker_queue = (
            WorkerQueue(job_manager.redis_client) if job_manager.redis_client else None
        )
        logger.info("Job manager initialized")
    else:
        app.state.job_manager = None
        app.state.worker_queue = None
        logger.info("Job manager not initialized (Redis not configured)")

    # Initialize stateless-first ethical crawling (uses Redis when available, memory otherwise)
    redis_client = app.state.job_manager.redis_client if app.state.job_manager else None
    app.state.robots_checker = RobotsChecker(
        user_agent=current_settings.crawling.user_agent,
        robots_cache_ttl=current_settings.crawling.robots_cache_ttl,
        robots_cache_max_size=current_settings.crawling.robots_cache_max_size,
        robots_fetch_timeout=current_settings.crawling.robots_fetch_timeout,
        max_crawl_delay=current_settings.crawling.max_crawl_delay,
        redis_client=redis_client,
    )
    app.state.domain_throttle = DomainThrottle(
        max_crawl_delay=current_settings.crawling.max_crawl_delay,
        max_size=current_settings.crawling.robots_cache_max_size,
        redis_client=redis_client,
    )

    # Initialize scheduler & executor after ethical crawling helpers are available
    if current_settings.redis.redis_uri and job_manager is not None:
        logger.info("Initializing scheduler...")
        scheduler_service = SchedulerService(
            redis_uri=current_settings.redis.redis_uri,
            settings=current_settings.scheduler,
        )
        # In split API mode, keep APScheduler paused so workers execute cron runs without duplicates
        await scheduler_service.start(paused=current_settings.worker.deployment_mode == "api")
        app.state.scheduler = scheduler_service
        logger.info("Scheduler initialized")

        from .routes.schedules import set_executor as set_schedules_executor
        from .scheduler import ExecutionStorage, ScheduledJobExecutor

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
        )
        scheduler_service.set_executor(executor)
        set_schedules_executor(executor)
        app.state.execution_storage = execution_storage
        logger.info("Scheduler executor initialized")
    else:
        app.state.scheduler = None
        app.state.execution_storage = None
        logger.info("Scheduler not initialized (Redis not configured)")

    # Start system metrics collection
    logger.info("Starting system metrics collection...")
    system_metrics = get_system_metrics_collector()
    await system_metrics.start(app_state=app.state)
    logger.info("System metrics collection started")

    yield

    # Graceful connection and job draining prior to tearing down upstream resources
    logger.info("Shutting down services (entering drain mode)...")
    drain_coordinator.start_drain()
    shutdown_start = time.monotonic()
    drain_timeout = current_settings.shutdown.drain_timeout
    await drain_coordinator.wait_until_drained(drain_timeout)

    if app.state.job_manager:
        remaining_drain = max(0.0, drain_timeout - (time.monotonic() - shutdown_start))
        await app.state.job_manager.wait_for_active_jobs(remaining_drain)

    # Stop metrics collection
    await system_metrics.stop()

    # Shutdown scheduler cleanly before closing HTTP/browser pools it may depend on
    if app.state.scheduler:
        await app.state.scheduler.shutdown(wait=True)

    # Close robots checker client
    if getattr(app.state, "robots_checker", None):
        await app.state.robots_checker.close()

    # Close HTTP client
    await http_client.close()

    # Close PDF generator if initialized and clear shared singleton
    if app.state.pdf_generator:
        await app.state.pdf_generator.close()
    await cleanup_pdf_generator()

    # Close job manager if initialized
    if app.state.job_manager:
        await app.state.job_manager.disconnect()

    shutdown_telemetry()
    drain_coordinator.reset()
    logger.info("All services shut down successfully")


# Create FastAPI app (interactive docs mounted explicitly below to return 404 in production)
app = FastAPI(
    title="REST API Downloader",
    description="High-performance web service for programmatic URL content downloading",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Add rate limiter & drain coordinator to app state and register exception handler
app.state.limiter = limiter
app.state.drain_coordinator = DrainCoordinator()
app.add_exception_handler(
    RateLimitExceeded,
    cast(ExceptionHandler, _rate_limit_exceeded_handler),
)

# Add middleware in reverse execution order: CORS -> Drain -> Metrics -> RateLimit -> Route
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


def _require_non_prod_docs(request: Request) -> Any:
    active_settings = getattr(request.app.state, "settings", None) or get_settings()
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


@app.get("/health", response_model=None)
async def health_check(request: Request) -> dict[str, Any] | Response:
    """Health check endpoint with configuration-aware monitoring and drain status."""
    # Get settings and semaphores from app state
    app_settings = request.app.state.settings
    pdf_semaphore = request.app.state.pdf_semaphore
    batch_semaphore = request.app.state.batch_semaphore
    drain_coordinator: DrainCoordinator | None = getattr(
        request.app.state, "drain_coordinator", None
    )
    is_draining = bool(drain_coordinator and drain_coordinator.is_draining)

    # Get concurrency limits from config
    pdf_limit = app_settings.pdf.concurrency
    batch_limit = app_settings.batch.concurrency

    # Check if Redis is available for batch processing
    job_manager: JobManager | None = getattr(request.app.state, "job_manager", None)
    redis_available = job_manager is not None

    # Check job manager status
    job_manager_status: dict[str, Any] = {
        "available": redis_available,
    }

    if job_manager:
        try:
            if job_manager.redis_client:
                await cast(Awaitable[bool], job_manager.redis_client.ping())
                job_manager_status["status"] = "connected"
            else:
                job_manager_status["status"] = "not_connected"
        except Exception as e:
            job_manager_status["status"] = "error"
            job_manager_status["error"] = str(e)
    else:
        job_manager_status["reason"] = "Redis connection required"

    # Calculate batch processing metrics
    batch_in_use = batch_limit - batch_semaphore._value
    batch_util = (batch_in_use / batch_limit) * 100 if batch_limit > 0 else 0

    batch_info: dict[str, Any] = {
        "available": redis_available,
    }

    if redis_available:
        batch_info.update(
            {
                "max_concurrent_downloads": batch_limit,
                "current_active_downloads": batch_in_use,
                "available_slots": batch_semaphore._value,
                "utilization_percent": round(batch_util, 1),
            }
        )
    else:
        batch_info["reason"] = "Redis connection (REDIS_URI) required"

    # Calculate PDF generation metrics
    pdf_in_use = pdf_limit - pdf_semaphore._value
    pdf_util = (pdf_in_use / pdf_limit) * 100 if pdf_limit > 0 else 0

    # Check if PDF generator is available
    pdf_generator = getattr(request.app.state, "pdf_generator", None)
    pdf_available = pdf_generator is not None

    # Check scheduler status
    scheduler: SchedulerService | None = getattr(request.app.state, "scheduler", None)
    scheduler_status: dict[str, Any] = {
        "available": scheduler is not None,
    }
    if scheduler:
        scheduler_status.update(await scheduler.get_status())
    else:
        scheduler_status["reason"] = "Redis connection required"

    # Check split worker heartbeats & queue depths when Redis is available
    workers_status: dict[str, Any] = {
        "deployment_mode": app_settings.worker.deployment_mode,
        "remote_browser_enabled": app_settings.worker.remote_browser_enabled,
    }
    worker_queue: WorkerQueue | None = getattr(request.app.state, "worker_queue", None)
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
        except Exception as exc:
            workers_status["error"] = str(exc)

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


# Include API router (must be after specific routes like /health and /docs)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("downloader.main:app", host="0.0.0.0", port=8000, reload=True)

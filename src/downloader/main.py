"""Main FastAPI application module."""

import asyncio
import multiprocessing
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from . import __version__
from .api import router
from .auth import get_auth_status
from .config import get_settings
from .http_client import HTTPClient
from .job_manager import JobManager
from .logging_config import get_logger, setup_logging
from .middleware import MetricsMiddleware, get_system_metrics_collector
from .pdf_generator import PlaywrightPDFGenerator
from .ratelimit_middleware import RateLimitMiddleware

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
async def lifespan(app: FastAPI):
    """Application lifespan events - initialize and cleanup resources."""
    # Store settings in app state (call get_settings() to support test env reloading)
    current_settings = get_settings()
    app.state.settings = current_settings

    # Log configuration validation messages
    logger.info(f"Starting {current_settings.app_name} v{current_settings.app_version}")
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
        max_concurrent=current_settings.http.max_concurrent_api,
        max_concurrent_batch=current_settings.http.max_concurrent_batch,
    )
    app.state.http_client = http_client
    logger.info("HTTP client initialized")

    # Initialize PDF generator with browser pool
    logger.info("Initializing PDF generator with browser pool...")
    try:
        pdf_generator = PlaywrightPDFGenerator(
            pool_size=current_settings.pdf.pool_size,
            page_load_timeout=current_settings.pdf.page_load_timeout,
            wait_until=current_settings.pdf.wait_until,
        )
        await pdf_generator.__aenter__()
        app.state.pdf_generator = pdf_generator
        logger.info(
            f"PDF generator initialized successfully (timeout={current_settings.pdf.page_load_timeout}ms, wait_until={current_settings.pdf.wait_until})"
        )
    except Exception as e:
        logger.warning(
            f"PDF generator initialization failed: {e}. PDF generation will be unavailable."
        )
        app.state.pdf_generator = None

    # Initialize semaphores for concurrency control
    app.state.pdf_semaphore = asyncio.Semaphore(current_settings.pdf.concurrency)
    app.state.batch_semaphore = asyncio.Semaphore(current_settings.batch.concurrency)
    logger.info(
        f"Concurrency semaphores initialized (PDF={current_settings.pdf.concurrency}, BATCH={current_settings.batch.concurrency})"
    )

    # Initialize job manager if Redis is configured
    if current_settings.redis.redis_uri:
        logger.info("Initializing job manager with Redis...")
        job_manager = JobManager(current_settings.redis.redis_uri)
        await job_manager.connect()
        app.state.job_manager = job_manager
        logger.info("Job manager initialized")
    else:
        app.state.job_manager = None
        logger.info("Job manager not initialized (Redis not configured)")

    # Start system metrics collection
    logger.info("Starting system metrics collection...")
    system_metrics = get_system_metrics_collector()
    await system_metrics.start(app_state=app.state)
    logger.info("System metrics collection started")

    yield

    # Shutdown - cleanup in reverse order
    logger.info("Shutting down services...")

    # Stop metrics collection
    await system_metrics.stop()

    # Close HTTP client
    await http_client.close()

    # Close PDF generator if initialized
    if app.state.pdf_generator:
        await app.state.pdf_generator.__aexit__(None, None, None)

    # Close job manager if initialized
    if app.state.job_manager:
        await app.state.job_manager.disconnect()

    logger.info("All services shut down successfully")


# Create FastAPI app
app = FastAPI(
    title="REST API Downloader",
    description="High-performance web service for programmatic URL content downloading",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add rate limiter to app state and register exception handler
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Add middleware in order: RateLimit first, then Metrics, then CORS
app.add_middleware(RateLimitMiddleware, limiter=limiter)
app.add_middleware(MetricsMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors.allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check(request: Request):
    """Health check endpoint with configuration-aware monitoring."""
    # Get settings and semaphores from app state
    app_settings = request.app.state.settings
    pdf_semaphore = request.app.state.pdf_semaphore
    batch_semaphore = request.app.state.batch_semaphore

    # Get concurrency limits from config
    pdf_limit = app_settings.pdf.concurrency
    batch_limit = app_settings.batch.concurrency

    # Check if Redis is available for batch processing
    job_manager = getattr(request.app.state, "job_manager", None)
    redis_available = job_manager is not None

    # Check job manager status
    job_manager_status = {
        "available": redis_available,
    }

    if job_manager:
        try:
            if job_manager.redis_client:
                await job_manager.redis_client.ping()
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

    batch_info = {
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

    health_info = {
        "status": "healthy",
        "version": __version__,
        "environment": app_settings.environment,
        "configuration": {
            "pdf_concurrency": pdf_limit,
            "batch_concurrency": batch_limit,
            "max_download_size_mb": app_settings.content.max_download_size / 1024 / 1024,
            "cpu_cores": multiprocessing.cpu_count(),
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
        },
    }
    health_info.update(get_auth_status(app_settings))
    return health_info


# Include API router (must be after specific routes like /health)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("downloader.main:app", host="0.0.0.0", port=8000, reload=True)

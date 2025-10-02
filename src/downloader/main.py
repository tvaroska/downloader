"""Main FastAPI application module."""

import asyncio
import multiprocessing
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .auth import get_auth_status
from .config import Settings, get_settings
from .http_client import HTTPClient
from .job_manager import JobManager
from .logging_config import get_logger, setup_logging
from .middleware import MetricsMiddleware, get_system_metrics_collector
from .pdf_generator import PlaywrightPDFGenerator

# Load settings and configure logging
settings = get_settings()
setup_logging(settings.logging)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events - initialize and cleanup resources."""
    # Store settings in app state
    app.state.settings = settings

    # Log configuration validation messages
    logger.info(f"Starting {settings.app_name} v{settings.app_version}")
    logger.info(f"Environment: {settings.environment}")
    for message in settings.validate_settings():
        if "WARNING" in message:
            logger.warning(message.replace("WARNING: ", ""))
        elif "ERROR" in message:
            logger.error(message.replace("ERROR: ", ""))
        else:
            logger.info(message.replace("INFO: ", ""))

    # Initialize HTTP client
    logger.info("Initializing HTTP client...")
    http_client = HTTPClient(
        timeout=settings.http.request_timeout,
        max_redirects=settings.http.max_redirects,
        max_concurrent=settings.http.max_concurrent_api,
        max_concurrent_batch=settings.http.max_concurrent_batch,
    )
    app.state.http_client = http_client
    logger.info("HTTP client initialized")

    # Initialize PDF generator with browser pool
    logger.info("Initializing PDF generator with browser pool...")
    pdf_generator = PlaywrightPDFGenerator()
    await pdf_generator.__aenter__()
    app.state.pdf_generator = pdf_generator
    logger.info("PDF generator initialized successfully")

    # Initialize semaphores for concurrency control
    app.state.pdf_semaphore = asyncio.Semaphore(settings.pdf.concurrency)
    app.state.batch_semaphore = asyncio.Semaphore(settings.batch.concurrency)
    logger.info(f"Concurrency semaphores initialized (PDF={settings.pdf.concurrency}, BATCH={settings.batch.concurrency})")

    # Initialize job manager if Redis is configured
    if settings.redis.redis_uri:
        logger.info("Initializing job manager with Redis...")
        job_manager = JobManager(settings.redis.redis_uri)
        await job_manager.connect()
        app.state.job_manager = job_manager
        logger.info("Job manager initialized")
    else:
        app.state.job_manager = None
        logger.info("Job manager not initialized (Redis not configured)")

    # Start system metrics collection
    logger.info("Starting system metrics collection...")
    system_metrics = get_system_metrics_collector()
    await system_metrics.start()
    logger.info("System metrics collection started")

    yield

    # Shutdown - cleanup in reverse order
    logger.info("Shutting down services...")

    # Stop metrics collection
    await system_metrics.stop()

    # Close HTTP client
    await http_client.close()

    # Close PDF generator
    await pdf_generator.__aexit__(None, None, None)

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

# Add middleware in order: Metrics first, then CORS
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
    job_manager = getattr(request.app.state, 'job_manager', None)
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
        "concurrency_limit": batch_limit,
        "current_active": batch_in_use,
        "available_slots": batch_semaphore._value,
        "utilization_percent": round(batch_util, 1),
    }

    if not redis_available:
        batch_info["reason"] = "Redis connection required"

    # Calculate PDF generation metrics
    pdf_in_use = pdf_limit - pdf_semaphore._value
    pdf_util = (pdf_in_use / pdf_limit) * 100 if pdf_limit > 0 else 0

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
                "available": True,
                "concurrency_limit": pdf_limit,
                "current_active": pdf_in_use,
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

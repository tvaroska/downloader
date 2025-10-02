"""Main FastAPI application module."""

import asyncio
import logging
import multiprocessing
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .auth import get_auth_status
from .http_client import HTTPClient
from .job_manager import JobManager
from .middleware import MetricsMiddleware, get_system_metrics_collector
from .pdf_generator import PlaywrightPDFGenerator

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def _get_optimal_concurrency_limits() -> tuple[int, int]:
    """Calculate optimal concurrency limits based on system resources."""
    cpu_count = multiprocessing.cpu_count()

    # PDF generation is CPU/memory intensive, conservative scaling
    default_pdf_limit = min(cpu_count * 2, 12)
    pdf_concurrency = int(os.getenv('PDF_CONCURRENCY', default_pdf_limit))

    # Batch processing is I/O bound, more aggressive scaling
    default_batch_limit = min(cpu_count * 8, 50)
    batch_concurrency = int(os.getenv('BATCH_CONCURRENCY', default_batch_limit))

    logger.info(f"Concurrency limits: PDF={pdf_concurrency}, BATCH={batch_concurrency} (CPU cores: {cpu_count})")

    return pdf_concurrency, batch_concurrency


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events - initialize and cleanup resources."""
    # Initialize HTTP client
    logger.info("Initializing HTTP client...")
    http_client = HTTPClient()
    app.state.http_client = http_client
    logger.info("HTTP client initialized")

    # Initialize PDF generator with browser pool
    logger.info("Initializing PDF generator with browser pool...")
    pdf_generator = PlaywrightPDFGenerator()
    await pdf_generator.__aenter__()
    app.state.pdf_generator = pdf_generator
    logger.info("PDF generator initialized successfully")

    # Initialize semaphores for concurrency control
    pdf_concurrency, batch_concurrency = _get_optimal_concurrency_limits()
    app.state.pdf_semaphore = asyncio.Semaphore(pdf_concurrency)
    app.state.batch_semaphore = asyncio.Semaphore(batch_concurrency)
    logger.info("Concurrency semaphores initialized")

    # Initialize job manager if Redis is configured
    if os.getenv("REDIS_URI"):
        logger.info("Initializing job manager with Redis...")
        job_manager = JobManager(os.getenv("REDIS_URI"))
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check(request: Request):
    """Health check endpoint with optimized concurrency monitoring."""
    import multiprocessing

    # Get semaphores from app state
    pdf_semaphore = request.app.state.pdf_semaphore
    batch_semaphore = request.app.state.batch_semaphore
    pdf_concurrency = pdf_semaphore._value + (12 - pdf_semaphore._value)  # Reconstruct limit
    batch_concurrency = batch_semaphore._value + (50 - batch_semaphore._value)  # Reconstruct limit

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
        job_manager_status["reason"] = "Redis connection (REDIS_URI) required"

    batch_in_use = 50 - batch_semaphore._value
    batch_util = (batch_in_use / 50) * 100 if 50 > 0 else 0

    batch_info = {
        "available": redis_available,
        "concurrency_limit": 50,
        "current_active": batch_in_use,
        "available_slots": batch_semaphore._value,
        "utilization_percent": round(batch_util, 1),
    }

    if not redis_available:
        batch_info["reason"] = "Redis connection (REDIS_URI) required"

    pdf_in_use = 12 - pdf_semaphore._value
    pdf_util = (pdf_in_use / 12) * 100 if 12 > 0 else 0

    health_info = {
        "status": "healthy",
        "version": __version__,
        "concurrency_optimization": {
            "enabled": True,
            "cpu_cores": multiprocessing.cpu_count(),
            "pdf_scaling": "2x CPU cores (max 12)",
            "batch_scaling": "8x CPU cores (max 50)",
        },
        "services": {
            "job_manager": job_manager_status,
            "batch_processing": batch_info,
            "pdf_generation": {
                "available": True,
                "concurrency_limit": 12,
                "current_active": pdf_in_use,
                "available_slots": pdf_semaphore._value,
                "utilization_percent": round(pdf_util, 1),
            },
        },
    }
    health_info.update(get_auth_status())
    return health_info


# Include API router (must be after specific routes like /health)
app.include_router(router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("downloader.main:app", host="0.0.0.0", port=8000, reload=True)

"""Main FastAPI application module."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .auth import get_auth_status
from .http_client import close_client
from .job_manager import cleanup_job_manager
from .pdf_generator import cleanup_pdf_generator, get_pdf_generator

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup - Initialize Playwright pool
    logger.info("Initializing Playwright browser pool...")
    async with get_pdf_generator() as generator:
        logger.info("Playwright browser pool initialized successfully")
        app.state.pdf_generator = generator
        yield
    # Shutdown
    await close_client()
    await cleanup_pdf_generator()
    await cleanup_job_manager()


# Create FastAPI app
app = FastAPI(
    title="REST API Downloader",
    description="High-performance web service for programmatic URL content downloading",
    version=__version__,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health_check():
    """Health check endpoint with optimized concurrency monitoring."""
    from .api import get_concurrency_stats

    # Get optimized concurrency statistics
    concurrency_stats = get_concurrency_stats()

    # Check if Redis is available for batch processing
    redis_available = bool(os.getenv("REDIS_URI"))

    # Check job manager status
    job_manager_status = {
        "available": redis_available,
    }

    if redis_available:
        try:
            from .job_manager import get_job_manager
            job_manager = await get_job_manager()
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

    batch_info = {
        "available": redis_available,
        "concurrency_limit": concurrency_stats["batch_concurrency"]["limit"],
        "current_active": concurrency_stats["batch_concurrency"]["in_use"],
        "available_slots": concurrency_stats["batch_concurrency"]["available"],
        "utilization_percent": concurrency_stats["batch_concurrency"]["utilization_percent"],
    }

    if not redis_available:
        batch_info["reason"] = "Redis connection (REDIS_URI) required"

    health_info = {
        "status": "healthy",
        "version": __version__,
        "concurrency_optimization": {
            "enabled": True,
            "cpu_cores": concurrency_stats["system_info"]["cpu_cores"],
            "pdf_scaling": concurrency_stats["system_info"]["pdf_scaling_factor"],
            "batch_scaling": concurrency_stats["system_info"]["batch_scaling_factor"],
        },
        "services": {
            "job_manager": job_manager_status,
            "batch_processing": batch_info,
            "pdf_generation": {
                "available": True,
                "concurrency_limit": concurrency_stats["pdf_concurrency"]["limit"],
                "current_active": concurrency_stats["pdf_concurrency"]["in_use"],
                "available_slots": concurrency_stats["pdf_concurrency"]["available"],
                "utilization_percent": concurrency_stats["pdf_concurrency"]["utilization_percent"],
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

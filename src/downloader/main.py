"""Main FastAPI application module."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import __version__
from .api import router
from .auth import get_auth_status
from .http_client import close_client
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
    """Health check endpoint."""
    from .api import BATCH_SEMAPHORE, PDF_SEMAPHORE

    # Get semaphore usage information
    batch_max = getattr(BATCH_SEMAPHORE, "_value", 20)
    batch_available = getattr(BATCH_SEMAPHORE, "_value", 20)
    batch_active = batch_max - batch_available

    pdf_max = getattr(PDF_SEMAPHORE, "_value", 5)
    pdf_available = getattr(PDF_SEMAPHORE, "_value", 5)
    pdf_active = pdf_max - pdf_available

    health_info = {
        "status": "healthy",
        "version": __version__,
        "services": {
            "batch_processing": {
                "available": True,
                "max_concurrent_downloads": batch_max,
                "current_active_downloads": batch_active,
                "available_slots": batch_available,
            },
            "pdf_generation": {
                "available": True,
                "max_concurrent_pdfs": pdf_max,
                "current_active_pdfs": pdf_active,
                "available_slots": pdf_available,
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

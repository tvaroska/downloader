"""
Legacy API module - provides backward compatibility.

This module now re-exports the new modular structure for backward compatibility.
New code should import from routes/ and services/ directly.
"""

import asyncio
import logging
import multiprocessing
import os

from fastapi import APIRouter

from .routes.batch import router as batch_router
from .routes.download import router as download_router
from .routes.metrics import router as metrics_router

logger = logging.getLogger(__name__)

# Create main router
router = APIRouter()

# Include all sub-routers
router.include_router(metrics_router, tags=["metrics"])
router.include_router(batch_router, tags=["batch"])
router.include_router(download_router, tags=["download"])


# Intelligent concurrency control with CPU-based defaults
def _get_optimal_concurrency_limits() -> tuple[int, int]:
    """Calculate optimal concurrency limits based on system resources."""
    cpu_count = multiprocessing.cpu_count()

    # PDF generation is CPU/memory intensive, conservative scaling
    default_pdf_limit = min(cpu_count * 2, 12)  # 2x CPU cores, max 12
    pdf_concurrency = int(os.getenv('PDF_CONCURRENCY', default_pdf_limit))

    # Batch processing is I/O bound, more aggressive scaling
    default_batch_limit = min(cpu_count * 8, 50)  # 8x CPU cores, max 50
    batch_concurrency = int(os.getenv('BATCH_CONCURRENCY', default_batch_limit))

    logger.info(f"Concurrency limits: PDF={pdf_concurrency}, BATCH={batch_concurrency} (CPU cores: {cpu_count})")

    return pdf_concurrency, batch_concurrency


# Initialize dynamic semaphores based on system resources
_pdf_concurrency, _batch_concurrency = _get_optimal_concurrency_limits()
PDF_SEMAPHORE = asyncio.Semaphore(_pdf_concurrency)
BATCH_SEMAPHORE = asyncio.Semaphore(_batch_concurrency)

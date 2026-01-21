"""
Legacy API module - provides backward compatibility.

This module now re-exports the new modular structure for backward compatibility.
New code should import from routes/ and services/ directly.
"""

from fastapi import APIRouter

from .routes.batch import router as batch_router
from .routes.download import router as download_router
from .routes.metrics import router as metrics_router
from .routes.schedules import router as schedule_router

# Create main router
router = APIRouter()

# Include all sub-routers
router.include_router(metrics_router, tags=["metrics"])
router.include_router(batch_router, tags=["batch"])
router.include_router(schedule_router, tags=["schedules"])
router.include_router(download_router, tags=["download"])

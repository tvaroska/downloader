"""Metrics and monitoring endpoints."""

import logging
import multiprocessing
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response

from ..dependencies import PDFSemaphoreDep, BatchSemaphoreDep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/metrics")
async def get_metrics():
    """Get Prometheus-formatted metrics for monitoring."""
    from ..metrics import get_metrics_collector

    collector = get_metrics_collector()
    return Response(
        content=collector.get_prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8"
    )


@router.get("/metrics/performance")
async def get_performance_metrics():
    """Get detailed performance metrics in JSON format."""
    from ..metrics import get_metrics_collector

    collector = get_metrics_collector()
    return collector.get_performance_summary()


@router.get("/metrics/health-score")
async def get_health_score():
    """Get system health score and diagnostics."""
    from ..metrics import get_metrics_collector

    collector = get_metrics_collector()
    return collector.get_system_health_score()


@router.get("/metrics/live")
async def get_live_metrics(
    request: Request,
    pdf_semaphore: PDFSemaphoreDep = None,
    batch_semaphore: BatchSemaphoreDep = None,
):
    """Get live system metrics including current concurrency and connection stats."""
    from ..metrics import get_metrics_collector

    collector = get_metrics_collector()
    performance = collector.get_performance_summary()

    # Calculate concurrency stats from semaphores
    pdf_limit = 12  # Max from config
    batch_limit = 50  # Max from config
    pdf_in_use = pdf_limit - pdf_semaphore._value
    batch_in_use = batch_limit - batch_semaphore._value

    # Try to get Redis stats
    redis_stats = {}
    try:
        job_manager = getattr(request.app.state, 'job_manager', None)
        if job_manager:
            redis_stats = await job_manager.get_connection_stats()
    except Exception as e:
        redis_stats = {"status": "error", "error": str(e)}

    # Try to get HTTP client stats
    http_stats = {}
    try:
        http_client = request.app.state.http_client
        http_stats = http_client.get_connection_stats()
    except Exception as e:
        http_stats = {"status": "error", "error": str(e)}

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "concurrency": {
            "pdf": {
                "limit": pdf_limit,
                "active": pdf_in_use,
                "available": pdf_semaphore._value,
                "utilization_percent": round((pdf_in_use / pdf_limit) * 100, 1) if pdf_limit > 0 else 0
            },
            "batch": {
                "limit": batch_limit,
                "active": batch_in_use,
                "available": batch_semaphore._value,
                "utilization_percent": round((batch_in_use / batch_limit) * 100, 1) if batch_limit > 0 else 0
            }
        },
        "performance": performance,
        "connections": {
            "redis": redis_stats,
            "http_client": http_stats
        },
        "system": {
            "cpu_cores": multiprocessing.cpu_count(),
            "pdf_scaling": "2x CPU cores (max 12)",
            "batch_scaling": "8x CPU cores (max 50)"
        }
    }

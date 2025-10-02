"""Metrics and monitoring endpoints."""

import logging
import multiprocessing
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Response

from ..models.responses import ConcurrencyInfo, ConcurrencyStats, SystemInfo

logger = logging.getLogger(__name__)

router = APIRouter()


def get_pdf_semaphore():
    """Get PDF semaphore from app state."""
    from ..api import PDF_SEMAPHORE, _pdf_concurrency
    return PDF_SEMAPHORE, _pdf_concurrency


def get_batch_semaphore():
    """Get batch semaphore from app state."""
    from ..api import BATCH_SEMAPHORE, _batch_concurrency
    return BATCH_SEMAPHORE, _batch_concurrency


def get_concurrency_stats() -> ConcurrencyStats:
    """Get current concurrency statistics for monitoring."""
    pdf_semaphore, pdf_concurrency = get_pdf_semaphore()
    batch_semaphore, batch_concurrency = get_batch_semaphore()

    pdf_info = ConcurrencyInfo(
        limit=pdf_concurrency,
        available=pdf_semaphore._value,
        in_use=pdf_concurrency - pdf_semaphore._value,
        utilization_percent=round(((pdf_concurrency - pdf_semaphore._value) / pdf_concurrency) * 100, 1)
    )

    batch_info = ConcurrencyInfo(
        limit=batch_concurrency,
        available=batch_semaphore._value,
        in_use=batch_concurrency - batch_semaphore._value,
        utilization_percent=round(((batch_concurrency - batch_semaphore._value) / batch_concurrency) * 100, 1)
    )

    system_info = SystemInfo(
        cpu_cores=multiprocessing.cpu_count(),
        pdf_scaling_factor="2x CPU cores (max 12)",
        batch_scaling_factor="8x CPU cores (max 50)"
    )

    return ConcurrencyStats(
        pdf_concurrency=pdf_info,
        batch_concurrency=batch_info,
        system_info=system_info
    )


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
async def get_live_metrics():
    """Get live system metrics including current concurrency and connection stats."""
    from ..metrics import get_metrics_collector

    collector = get_metrics_collector()
    concurrency_stats = get_concurrency_stats()

    performance = collector.get_performance_summary()

    # Try to get Redis stats
    redis_stats = {}
    try:
        if os.getenv("REDIS_URI"):
            from ..job_manager import get_job_manager
            job_manager = await get_job_manager()
            if job_manager:
                redis_stats = await job_manager.get_connection_stats()
    except Exception as e:
        redis_stats = {"status": "error", "error": str(e)}

    # Try to get HTTP client stats
    http_stats = {}
    try:
        from ..http_client import get_client
        client = await get_client()
        http_stats = client.get_connection_stats()
    except Exception as e:
        http_stats = {"status": "error", "error": str(e)}

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "concurrency": {
            "pdf": {
                "limit": concurrency_stats.pdf_concurrency.limit,
                "active": concurrency_stats.pdf_concurrency.in_use,
                "available": concurrency_stats.pdf_concurrency.available,
                "utilization_percent": concurrency_stats.pdf_concurrency.utilization_percent
            },
            "batch": {
                "limit": concurrency_stats.batch_concurrency.limit,
                "active": concurrency_stats.batch_concurrency.in_use,
                "available": concurrency_stats.batch_concurrency.available,
                "utilization_percent": concurrency_stats.batch_concurrency.utilization_percent
            }
        },
        "performance": performance,
        "connections": {
            "redis": redis_stats,
            "http_client": http_stats
        },
        "system": {
            "cpu_cores": concurrency_stats.system_info.cpu_cores,
            "pdf_scaling": concurrency_stats.system_info.pdf_scaling_factor,
            "batch_scaling": concurrency_stats.system_info.batch_scaling_factor
        }
    }

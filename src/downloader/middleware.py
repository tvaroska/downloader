"""Middleware for request metrics collection and background system gauge snapshots."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

import redis.exceptions as redis_exc
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .metrics import get_metrics_collector
from .telemetry import get_current_trace_context

logger = logging.getLogger(__name__)

_EXACT_METRIC_PATHS = frozenset(
    {
        "/",
        "/render",
        "/health",
        "/schedules",
        "/metrics",
        "/metrics/performance",
        "/metrics/health-score",
        "/metrics/live",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)
_PREFIX_METRIC_PATHS: tuple[tuple[str, str], ...] = (
    ("/batch", "/batch"),
    ("/metrics/", "/metrics"),
    ("/jobs/", "/jobs/{job_id}"),
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware recording per-route request latency, error counters, and `X-Trace-ID`."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        start_time = time.perf_counter()
        method = request.method
        normalized_path = self._normalize_path(request.url.path)

        try:
            response: Response = await call_next(request)
        except Exception:
            response_time = time.perf_counter() - start_time
            get_metrics_collector().record_request(
                endpoint=normalized_path,
                method=method,
                status_code=500,
                response_time=response_time,
            )
            raise

        response_time = time.perf_counter() - start_time
        get_metrics_collector().record_request(
            endpoint=normalized_path,
            method=method,
            status_code=response.status_code,
            response_time=response_time,
        )
        response.headers["X-Response-Time"] = f"{response_time:.3f}s"
        trace_id, _ = get_current_trace_context()
        if trace_id:
            response.headers.setdefault("X-Trace-ID", trace_id)
        return response

    def _normalize_path(self, path: str) -> str:
        if path in _EXACT_METRIC_PATHS:
            return path
        if path.startswith("/schedules/"):
            return (
                "/schedules/{schedule_id}/history"
                if path.endswith("/history")
                else "/schedules/{schedule_id}"
            )
        for prefix, bucket in _PREFIX_METRIC_PATHS:
            if path.startswith(prefix):
                return bucket
        return "/download"


class SystemMetricsCollector:
    """Collects semaphore, Redis, and HTTP pool gauges periodically in the background."""

    def __init__(self) -> None:
        self.collector = get_metrics_collector()
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self.app_state: Any = None

    async def start(self, app_state: Any = None) -> None:
        self.app_state = app_state
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._collect_system_metrics())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _collect_system_metrics(self) -> None:
        while self._running:
            try:
                await self._collect_metrics_snapshot()
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # Isolate unexpected gauge errors so the background loop never dies silently.
                logger.error(f"Error collecting system metrics: {exc}")
                await asyncio.sleep(60)

    async def _collect_metrics_snapshot(self) -> None:
        if self.app_state:
            pdf_semaphore = getattr(self.app_state, "pdf_semaphore", None)
            batch_semaphore = getattr(self.app_state, "batch_semaphore", None)
            settings = getattr(self.app_state, "settings", None)
            if pdf_semaphore and batch_semaphore and settings:
                pdf_limit = settings.pdf.concurrency
                batch_limit = settings.batch.concurrency
                pdf_in_use = pdf_limit - pdf_semaphore._value
                batch_in_use = batch_limit - batch_semaphore._value
                pdf_util = (pdf_in_use / pdf_limit * 100) if pdf_limit > 0 else 0.0
                batch_util = (batch_in_use / batch_limit * 100) if batch_limit > 0 else 0.0
                self.collector.set_gauge("pdf_concurrency_utilization", pdf_util)
                self.collector.set_gauge("batch_concurrency_utilization", batch_util)
                self.collector.set_gauge("pdf_concurrency_active", pdf_in_use)
                self.collector.set_gauge("batch_concurrency_active", batch_in_use)

        await self._collect_redis_metrics()
        await self._collect_pdf_pool_metrics()
        await self._collect_http_client_metrics()

    async def _collect_redis_metrics(self) -> None:
        try:
            job_manager = getattr(self.app_state, "job_manager", None) if self.app_state else None
            if job_manager and hasattr(job_manager, "get_connection_stats"):
                stats = await job_manager.get_connection_stats()
                if stats.get("status") == "healthy":
                    self.collector.set_gauge("redis_status", 1)
                    for key in (
                        "created_connections",
                        "available_connections",
                        "in_use_connections",
                    ):
                        if key in stats:
                            self.collector.set_gauge(f"redis_{key}", stats[key])
                else:
                    self.collector.set_gauge("redis_status", 0)
        except (redis_exc.RedisError, OSError, RuntimeError, TypeError):
            self.collector.set_gauge("redis_status", 0)

    async def _collect_pdf_pool_metrics(self) -> None:
        if not self.app_state:
            self.collector.set_gauge("pdf_pool_available", 0)
            return
        pdf_gen = getattr(self.app_state, "pdf_generator", None)
        if pdf_gen is None:
            self.collector.set_gauge("pdf_pool_available", 0)
            return

        available = getattr(pdf_gen, "available_browsers", None)
        if isinstance(available, int) and not isinstance(available, bool):
            self.collector.set_gauge("pdf_pool_available", max(0, available))
            return

        pdf_semaphore = getattr(self.app_state, "pdf_semaphore", None)
        sem_value = getattr(pdf_semaphore, "_value", None) if pdf_semaphore is not None else None
        if isinstance(sem_value, int) and not isinstance(sem_value, bool):
            self.collector.set_gauge("pdf_pool_available", max(0, sem_value))
            return

        self.collector.set_gauge("pdf_pool_available", 1)

    async def _collect_http_client_metrics(self) -> None:
        try:
            http_client = getattr(self.app_state, "http_client", None) if self.app_state else None
            if http_client and hasattr(http_client, "get_connection_stats"):
                stats = http_client.get_connection_stats()
                is_healthy = stats.get("status") == "healthy"
                self.collector.set_gauge("http_client_status", 1 if is_healthy else 0)
        except (OSError, RuntimeError, ValueError, TypeError):
            self.collector.set_gauge("http_client_status", 0)


_system_metrics_collector: SystemMetricsCollector | None = None


def get_system_metrics_collector() -> SystemMetricsCollector:
    global _system_metrics_collector
    if _system_metrics_collector is None:
        _system_metrics_collector = SystemMetricsCollector()
    return _system_metrics_collector

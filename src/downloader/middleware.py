"""Middleware for request metrics collection and monitoring."""

import time
import asyncio
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .metrics import get_metrics_collector


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware to collect request metrics automatically."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and collect metrics."""
        start_time = time.time()

        # Extract endpoint info
        method = request.method
        path = request.url.path

        # Normalize path for metrics (remove dynamic segments)
        normalized_path = self._normalize_path(path)

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception as e:
            # Record error metrics
            response_time = time.time() - start_time
            get_metrics_collector().record_request(
                endpoint=normalized_path,
                method=method,
                status_code=500,
                response_time=response_time
            )
            raise e

        # Calculate response time
        response_time = time.time() - start_time

        # Record metrics
        get_metrics_collector().record_request(
            endpoint=normalized_path,
            method=method,
            status_code=status_code,
            response_time=response_time
        )

        # Add response time header for debugging
        response.headers["X-Response-Time"] = f"{response_time:.3f}s"

        return response

    def _normalize_path(self, path: str) -> str:
        """Normalize URL path for metrics grouping."""
        # Handle different endpoint patterns
        if path.startswith("/batch"):
            return "/batch"
        elif path == "/health":
            return "/health"
        elif path == "/metrics":
            return "/metrics"
        elif path.startswith("/jobs/"):
            return "/jobs/{job_id}"
        elif path.startswith("/status/"):
            return "/status/{job_id}"
        elif path.startswith("/results/"):
            return "/results/{job_id}"
        elif path == "/":
            return "/"
        else:
            # For URL downloads - group as /download
            return "/download"


class SystemMetricsCollector:
    """Collects system-level metrics in the background."""

    def __init__(self):
        self.collector = get_metrics_collector()
        self._running = False
        self._task = None

    async def start(self):
        """Start background metrics collection."""
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._collect_system_metrics())

    async def stop(self):
        """Stop background metrics collection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _collect_system_metrics(self):
        """Collect system metrics periodically."""
        while self._running:
            try:
                await self._collect_metrics_snapshot()
                await asyncio.sleep(30)  # Collect every 30 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Log error but continue
                import logging
                logging.getLogger(__name__).error(f"Error collecting system metrics: {e}")
                await asyncio.sleep(60)  # Wait longer on error

    async def _collect_metrics_snapshot(self):
        """Collect a snapshot of current system metrics."""
        try:
            # Collect concurrency metrics
            from .api import get_concurrency_stats
            concurrency_stats = get_concurrency_stats()

            # Set gauge metrics
            self.collector.set_gauge("pdf_concurrency_utilization",
                                   concurrency_stats.pdf_concurrency.utilization_percent)
            self.collector.set_gauge("batch_concurrency_utilization",
                                   concurrency_stats.batch_concurrency.utilization_percent)
            self.collector.set_gauge("pdf_concurrency_active",
                                   concurrency_stats.pdf_concurrency.in_use)
            self.collector.set_gauge("batch_concurrency_active",
                                   concurrency_stats.batch_concurrency.in_use)

            # Collect Redis metrics if available
            await self._collect_redis_metrics()

            # Collect PDF pool metrics if available
            await self._collect_pdf_pool_metrics()

            # Collect HTTP client metrics
            await self._collect_http_client_metrics()

        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"Error in metrics snapshot: {e}")

    async def _collect_redis_metrics(self):
        """Collect Redis-specific metrics."""
        try:
            import os
            if os.getenv("REDIS_URI"):
                from .job_manager import get_job_manager
                job_manager = await get_job_manager()
                if job_manager:
                    stats = await job_manager.get_connection_stats()
                    if stats.get("status") == "healthy":
                        self.collector.set_gauge("redis_status", 1)
                        if "created_connections" in stats:
                            self.collector.set_gauge("redis_created_connections", stats["created_connections"])
                        if "available_connections" in stats:
                            self.collector.set_gauge("redis_available_connections", stats["available_connections"])
                        if "in_use_connections" in stats:
                            self.collector.set_gauge("redis_in_use_connections", stats["in_use_connections"])
                    else:
                        self.collector.set_gauge("redis_status", 0)
        except Exception:
            self.collector.set_gauge("redis_status", 0)

    async def _collect_pdf_pool_metrics(self):
        """Collect PDF browser pool metrics."""
        try:
            from .pdf_generator import BrowserPool
            # This would need to be implemented in pdf_generator to expose pool stats
            # For now, just set a placeholder
            self.collector.set_gauge("pdf_pool_available", 1)  # Placeholder
        except Exception:
            pass

    async def _collect_http_client_metrics(self):
        """Collect HTTP client metrics."""
        try:
            from .http_client import get_client
            client = await get_client()  # Fix: await the async function
            stats = client.get_connection_stats()

            if stats.get("status") == "healthy":
                self.collector.set_gauge("http_client_status", 1)

                # Extract circuit breaker stats
                circuit_breakers = stats.get("circuit_breakers", {})
                for url, cb_stats in circuit_breakers.items():
                    # Normalize URL for metric name
                    normalized_url = url.replace(".", "_").replace(":", "_")
                    self.collector.set_gauge(f"circuit_breaker_{normalized_url}_failures",
                                           cb_stats.get("failure_count", 0))
                    self.collector.set_gauge(f"circuit_breaker_{normalized_url}_state",
                                           1 if cb_stats.get("state") == "closed" else 0)
            else:
                self.collector.set_gauge("http_client_status", 0)
        except Exception:
            self.collector.set_gauge("http_client_status", 0)


# Global system metrics collector
_system_metrics_collector = None


def get_system_metrics_collector() -> SystemMetricsCollector:
    """Get the global system metrics collector."""
    global _system_metrics_collector
    if _system_metrics_collector is None:
        _system_metrics_collector = SystemMetricsCollector()
    return _system_metrics_collector
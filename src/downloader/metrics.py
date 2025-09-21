"""Enhanced metrics collection system for production monitoring."""

import asyncio
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
import logging

logger = logging.getLogger(__name__)


@dataclass
class MetricSnapshot:
    """A point-in-time snapshot of a metric."""
    timestamp: float
    value: float
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class PerformanceMetrics:
    """Performance metrics container."""
    request_count: int = 0
    error_count: int = 0
    total_response_time: float = 0.0
    min_response_time: float = float('inf')
    max_response_time: float = 0.0
    response_times: deque = field(default_factory=lambda: deque(maxlen=1000))  # Last 1000 requests

    @property
    def avg_response_time(self) -> float:
        """Calculate average response time."""
        if self.request_count == 0:
            return 0.0
        return self.total_response_time / self.request_count

    @property
    def error_rate(self) -> float:
        """Calculate error rate as percentage."""
        if self.request_count == 0:
            return 0.0
        return (self.error_count / self.request_count) * 100

    @property
    def p95_response_time(self) -> float:
        """Calculate 95th percentile response time."""
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        p95_index = int(len(sorted_times) * 0.95)
        return sorted_times[p95_index] if p95_index < len(sorted_times) else sorted_times[-1]


class MetricsCollector:
    """Centralized metrics collection system."""

    def __init__(self, history_size: int = 10000):
        self.history_size = history_size
        self._metrics_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=history_size))
        self._performance_metrics: Dict[str, PerformanceMetrics] = defaultdict(PerformanceMetrics)
        self._start_time = time.time()

        # Counter metrics
        self._counters: Dict[str, float] = defaultdict(float)

        # Gauge metrics (current values)
        self._gauges: Dict[str, float] = defaultdict(float)

        # Histogram buckets for response times
        self._histogram_buckets = [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float('inf')]
        self._histograms: Dict[str, Dict[float, int]] = defaultdict(lambda: {bucket: 0 for bucket in self._histogram_buckets})

    def record_request(self, endpoint: str, method: str, status_code: int, response_time: float):
        """Record a request with its metrics."""
        key = f"{method}_{endpoint}"

        # Update performance metrics
        perf = self._performance_metrics[key]
        perf.request_count += 1
        perf.total_response_time += response_time
        perf.min_response_time = min(perf.min_response_time, response_time)
        perf.max_response_time = max(perf.max_response_time, response_time)
        perf.response_times.append(response_time)

        if status_code >= 400:
            perf.error_count += 1

        # Update counters
        self._counters[f"requests_total_{key}"] += 1
        self._counters[f"requests_total_status_{status_code}"] += 1

        if status_code >= 400:
            self._counters[f"errors_total_{key}"] += 1

        # Update histograms
        for bucket in self._histogram_buckets:
            if response_time <= bucket:
                self._histograms[f"response_time_{key}"][bucket] += 1

        # Store in history
        timestamp = time.time()
        self._metrics_history[f"response_time_{key}"].append(
            MetricSnapshot(timestamp, response_time, {"method": method, "endpoint": endpoint, "status": str(status_code)})
        )

        logger.debug(f"Recorded request: {method} {endpoint} - {status_code} in {response_time:.3f}s")

    def set_gauge(self, name: str, value: float):
        """Set a gauge metric value."""
        self._gauges[name] = value
        timestamp = time.time()
        self._metrics_history[name].append(MetricSnapshot(timestamp, value))

    def increment_counter(self, name: str, value: float = 1.0):
        """Increment a counter metric."""
        self._counters[name] += value

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get a summary of performance metrics."""
        summary = {
            "uptime_seconds": time.time() - self._start_time,
            "total_requests": sum(perf.request_count for perf in self._performance_metrics.values()),
            "total_errors": sum(perf.error_count for perf in self._performance_metrics.values()),
            "endpoints": {}
        }

        for endpoint, perf in self._performance_metrics.items():
            summary["endpoints"][endpoint] = {
                "request_count": perf.request_count,
                "error_count": perf.error_count,
                "error_rate_percent": perf.error_rate,
                "avg_response_time": perf.avg_response_time,
                "min_response_time": perf.min_response_time if perf.min_response_time != float('inf') else 0,
                "max_response_time": perf.max_response_time,
                "p95_response_time": perf.p95_response_time
            }

        # Calculate overall metrics
        if summary["total_requests"] > 0:
            summary["overall_error_rate_percent"] = (summary["total_errors"] / summary["total_requests"]) * 100
            total_response_time = sum(perf.total_response_time for perf in self._performance_metrics.values())
            summary["overall_avg_response_time"] = total_response_time / summary["total_requests"]
        else:
            summary["overall_error_rate_percent"] = 0.0
            summary["overall_avg_response_time"] = 0.0

        return summary

    def get_prometheus_metrics(self) -> str:
        """Generate Prometheus-formatted metrics."""
        lines = []

        # Add metadata
        lines.append("# HELP downloader_uptime_seconds Time since service started")
        lines.append("# TYPE downloader_uptime_seconds gauge")
        lines.append(f"downloader_uptime_seconds {time.time() - self._start_time}")
        lines.append("")

        # Request counters
        lines.append("# HELP downloader_requests_total Total number of requests")
        lines.append("# TYPE downloader_requests_total counter")
        for name, value in self._counters.items():
            if name.startswith("requests_total_"):
                endpoint = name.replace("requests_total_", "")
                lines.append(f'downloader_requests_total{{endpoint="{endpoint}"}} {value}')
        lines.append("")

        # Error counters
        lines.append("# HELP downloader_errors_total Total number of errors")
        lines.append("# TYPE downloader_errors_total counter")
        for name, value in self._counters.items():
            if name.startswith("errors_total_"):
                endpoint = name.replace("errors_total_", "")
                lines.append(f'downloader_errors_total{{endpoint="{endpoint}"}} {value}')
        lines.append("")

        # Response time histograms
        lines.append("# HELP downloader_response_time_seconds Response time histogram")
        lines.append("# TYPE downloader_response_time_seconds histogram")
        for endpoint, buckets in self._histograms.items():
            if endpoint.startswith("response_time_"):
                endpoint_name = endpoint.replace("response_time_", "")
                for bucket, count in buckets.items():
                    bucket_str = "+Inf" if bucket == float('inf') else str(bucket)
                    lines.append(f'downloader_response_time_seconds_bucket{{endpoint="{endpoint_name}",le="{bucket_str}"}} {count}')
        lines.append("")

        # Current gauges
        lines.append("# HELP downloader_gauge Current gauge values")
        lines.append("# TYPE downloader_gauge gauge")
        for name, value in self._gauges.items():
            lines.append(f'downloader_gauge{{name="{name}"}} {value}')

        return "\n".join(lines)

    def get_recent_metrics(self, metric_name: str, seconds: int = 300) -> List[MetricSnapshot]:
        """Get recent metrics for a specific metric within the last N seconds."""
        cutoff_time = time.time() - seconds
        recent_metrics = []

        if metric_name in self._metrics_history:
            for snapshot in self._metrics_history[metric_name]:
                if snapshot.timestamp >= cutoff_time:
                    recent_metrics.append(snapshot)

        return recent_metrics

    def get_system_health_score(self) -> Dict[str, Any]:
        """Calculate an overall system health score."""
        performance = self.get_performance_summary()

        # Health scoring (0-100)
        health_score = 100
        health_details = {
            "overall_score": health_score,
            "factors": {}
        }

        # Error rate impact (max -30 points)
        error_rate = performance.get("overall_error_rate_percent", 0)
        if error_rate > 5:
            error_penalty = min(30, error_rate * 2)  # 2 points per percent over 5%
            health_score -= error_penalty
            health_details["factors"]["error_rate"] = {
                "value": error_rate,
                "penalty": error_penalty,
                "status": "critical" if error_rate > 10 else "warning"
            }

        # Response time impact (max -25 points)
        avg_response_time = performance.get("overall_avg_response_time", 0)
        if avg_response_time > 1.0:
            response_penalty = min(25, (avg_response_time - 1.0) * 10)  # 10 points per second over 1s
            health_score -= response_penalty
            health_details["factors"]["response_time"] = {
                "value": avg_response_time,
                "penalty": response_penalty,
                "status": "critical" if avg_response_time > 3.0 else "warning"
            }

        # Uptime bonus
        uptime = performance.get("uptime_seconds", 0)
        if uptime > 86400:  # More than 24 hours
            health_details["factors"]["uptime"] = {
                "value": uptime,
                "bonus": 0,
                "status": "good"
            }

        health_details["overall_score"] = max(0, health_score)
        health_details["status"] = (
            "healthy" if health_score >= 80 else
            "degraded" if health_score >= 60 else
            "unhealthy"
        )

        return health_details


# Global metrics collector instance
_metrics_collector: Optional[MetricsCollector] = None


def get_metrics_collector() -> MetricsCollector:
    """Get the global metrics collector instance."""
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


# Convenience functions
def record_request(endpoint: str, method: str, status_code: int, response_time: float):
    """Record a request metric."""
    get_metrics_collector().record_request(endpoint, method, status_code, response_time)


def set_gauge(name: str, value: float):
    """Set a gauge metric."""
    get_metrics_collector().set_gauge(name, value)


def increment_counter(name: str, value: float = 1.0):
    """Increment a counter metric."""
    get_metrics_collector().increment_counter(name, value)
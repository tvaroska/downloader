"""Metrics collection and Prometheus text exposition."""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MetricSnapshot:
    """A point-in-time snapshot of a metric."""

    timestamp: float
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class PerformanceMetrics:
    """Per-endpoint latency and error aggregator."""

    request_count: int = 0
    error_count: int = 0
    total_response_time: float = 0.0
    min_response_time: float = float("inf")
    max_response_time: float = 0.0
    response_times: deque[float] = field(default_factory=lambda: deque(maxlen=1000))

    @property
    def avg_response_time(self) -> float:
        if self.request_count == 0:
            return 0.0
        return self.total_response_time / self.request_count

    @property
    def error_rate(self) -> float:
        if self.request_count == 0:
            return 0.0
        return (self.error_count / self.request_count) * 100

    @property
    def p95_response_time(self) -> float:
        if not self.response_times:
            return 0.0
        sorted_times = sorted(self.response_times)
        p95_index = int(len(sorted_times) * 0.95)
        return sorted_times[p95_index] if p95_index < len(sorted_times) else sorted_times[-1]


class MetricsCollector:
    """Centralized in-process metrics collector."""

    def __init__(self, history_size: int = 10000) -> None:
        self.history_size = history_size
        self._metrics_history: dict[str, deque[MetricSnapshot]] = defaultdict(
            lambda: deque(maxlen=history_size)
        )
        self._performance_metrics: dict[str, PerformanceMetrics] = defaultdict(PerformanceMetrics)
        self._start_time = time.time()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = defaultdict(float)
        self._histogram_buckets = [0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, float("inf")]
        self._histograms: dict[str, dict[float, int]] = defaultdict(
            lambda: dict.fromkeys(self._histogram_buckets, 0)
        )

    def record_request(
        self,
        endpoint: str,
        method: str,
        status_code: int,
        response_time: float,
    ) -> None:
        key = f"{method}_{endpoint}"
        perf = self._performance_metrics[key]
        perf.request_count += 1
        perf.total_response_time += response_time
        perf.min_response_time = min(perf.min_response_time, response_time)
        perf.max_response_time = max(perf.max_response_time, response_time)
        perf.response_times.append(response_time)

        if status_code >= 400:
            perf.error_count += 1
            self._counters[f"errors_total_{key}"] += 1

        self._counters[f"requests_total_{key}"] += 1
        self._counters[f"requests_total_status_{status_code}"] += 1

        for bucket in self._histogram_buckets:
            if response_time <= bucket:
                self._histograms[f"response_time_{key}"][bucket] += 1

        self._metrics_history[f"response_time_{key}"].append(
            MetricSnapshot(
                time.time(),
                response_time,
                {"method": method, "endpoint": endpoint, "status": str(status_code)},
            )
        )

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value
        self._metrics_history[name].append(MetricSnapshot(time.time(), value))

    def increment_counter(self, name: str, value: float = 1.0) -> None:
        self._counters[name] += value

    def get_performance_summary(self) -> dict[str, Any]:
        total_requests = sum(perf.request_count for perf in self._performance_metrics.values())
        total_errors = sum(perf.error_count for perf in self._performance_metrics.values())
        endpoints: dict[str, dict[str, float | int]] = {}
        summary: dict[str, Any] = {
            "uptime_seconds": time.time() - self._start_time,
            "total_requests": total_requests,
            "total_errors": total_errors,
            "endpoints": endpoints,
            "crawling": {
                "robots_evaluations_total": int(self._counters.get("robots_evaluations_total", 0)),
                "robots_cache_hits_total": int(self._counters.get("robots_cache_hits_total", 0)),
                "robots_disallowed_total": int(self._counters.get("robots_disallowed_total", 0)),
            },
        }

        for endpoint, perf in self._performance_metrics.items():
            endpoints[endpoint] = {
                "request_count": perf.request_count,
                "error_count": perf.error_count,
                "error_rate_percent": perf.error_rate,
                "avg_response_time": perf.avg_response_time,
                "min_response_time": perf.min_response_time
                if perf.min_response_time != float("inf")
                else 0,
                "max_response_time": perf.max_response_time,
                "p95_response_time": perf.p95_response_time,
            }

        if total_requests > 0:
            summary["overall_error_rate_percent"] = (total_errors / total_requests) * 100
            total_response_time = sum(
                perf.total_response_time for perf in self._performance_metrics.values()
            )
            summary["overall_avg_response_time"] = total_response_time / total_requests
        else:
            summary["overall_error_rate_percent"] = 0.0
            summary["overall_avg_response_time"] = 0.0

        return summary

    def get_prometheus_metrics(self) -> str:
        lines = [
            "# HELP downloader_uptime_seconds Time since service started",
            "# TYPE downloader_uptime_seconds gauge",
            f"downloader_uptime_seconds {time.time() - self._start_time}",
            "",
            "# HELP downloader_requests_total Total number of requests",
            "# TYPE downloader_requests_total counter",
        ]
        for name, value in self._counters.items():
            if name.startswith("requests_total_"):
                endpoint = name.removeprefix("requests_total_")
                lines.append(f'downloader_requests_total{{endpoint="{endpoint}"}} {value}')
        lines.extend(
            [
                "",
                "# HELP downloader_errors_total Total number of errors",
                "# TYPE downloader_errors_total counter",
            ]
        )
        for name, value in self._counters.items():
            if name.startswith("errors_total_"):
                endpoint = name.removeprefix("errors_total_")
                lines.append(f'downloader_errors_total{{endpoint="{endpoint}"}} {value}')
        lines.extend(
            [
                "",
                "# HELP downloader_response_time_seconds Response time histogram",
                "# TYPE downloader_response_time_seconds histogram",
            ]
        )
        for endpoint, buckets in self._histograms.items():
            if endpoint.startswith("response_time_"):
                endpoint_name = endpoint.removeprefix("response_time_")
                for bucket, count in buckets.items():
                    bucket_str = "+Inf" if bucket == float("inf") else str(bucket)
                    lines.append(
                        f'downloader_response_time_seconds_bucket{{endpoint="{endpoint_name}",le="{bucket_str}"}} {count}'
                    )
        lines.extend(
            [
                "",
                "# HELP downloader_gauge Current gauge values",
                "# TYPE downloader_gauge gauge",
            ]
        )
        for name, value in self._gauges.items():
            lines.append(f'downloader_gauge{{name="{name}"}} {value}')
        lines.extend(
            [
                "",
                "# HELP robots_evaluations_total Total robots.txt evaluations",
                "# TYPE robots_evaluations_total counter",
                f"robots_evaluations_total {int(self._counters.get('robots_evaluations_total', 0))}",
                "# HELP robots_cache_hits_total Total robots.txt cache hits",
                "# TYPE robots_cache_hits_total counter",
                f"robots_cache_hits_total {int(self._counters.get('robots_cache_hits_total', 0))}",
                "# HELP robots_disallowed_total Total URLs disallowed by robots.txt",
                "# TYPE robots_disallowed_total counter",
                f"robots_disallowed_total {int(self._counters.get('robots_disallowed_total', 0))}",
            ]
        )
        return "\n".join(lines)

    def get_system_health_score(self) -> dict[str, Any]:
        performance = self.get_performance_summary()
        health_score: float = 100
        factors: dict[str, dict[str, Any]] = {}
        health_details: dict[str, Any] = {"overall_score": health_score, "factors": factors}

        error_rate = performance.get("overall_error_rate_percent", 0)
        if error_rate > 5:
            error_penalty = min(30, error_rate * 2)
            health_score -= error_penalty
            factors["error_rate"] = {
                "value": error_rate,
                "penalty": error_penalty,
                "status": "critical" if error_rate > 10 else "warning",
            }

        avg_response_time = performance.get("overall_avg_response_time", 0)
        if avg_response_time > 1.0:
            response_penalty = min(25, (avg_response_time - 1.0) * 10)
            health_score -= response_penalty
            factors["response_time"] = {
                "value": avg_response_time,
                "penalty": response_penalty,
                "status": "critical" if avg_response_time > 3.0 else "warning",
            }

        uptime = performance.get("uptime_seconds", 0)
        if uptime > 86400:
            factors["uptime"] = {"value": uptime, "bonus": 0, "status": "good"}

        health_details["overall_score"] = max(0, health_score)
        health_details["status"] = (
            "healthy" if health_score >= 80 else "degraded" if health_score >= 60 else "unhealthy"
        )
        return health_details


_metrics_collector: MetricsCollector | None = None


def get_metrics_collector() -> MetricsCollector:
    global _metrics_collector
    if _metrics_collector is None:
        _metrics_collector = MetricsCollector()
    return _metrics_collector


def record_request(endpoint: str, method: str, status_code: int, response_time: float) -> None:
    get_metrics_collector().record_request(endpoint, method, status_code, response_time)


def set_gauge(name: str, value: float) -> None:
    get_metrics_collector().set_gauge(name, value)


def increment_counter(name: str, value: float = 1.0) -> None:
    get_metrics_collector().increment_counter(name, value)


def record_html_rendering_detection() -> None:
    increment_counter("html_rendering_detections_total")


def record_html_rendering_duration(duration_seconds: float) -> None:
    collector = get_metrics_collector()
    for bucket in collector._histogram_buckets:
        if duration_seconds <= bucket:
            collector._histograms["html_rendering_duration_seconds"][bucket] += 1
    set_gauge("html_rendering_latest_duration_seconds", duration_seconds)


def record_html_rendering_failure() -> None:
    increment_counter("html_rendering_failures_total")


def record_html_rendering_success(original_size: int, rendered_size: int) -> None:
    increment_counter("html_rendering_successes_total")
    if original_size > 0:
        set_gauge("html_rendering_latest_size_ratio", rendered_size / original_size)


def record_robots_evaluation() -> None:
    increment_counter("robots_evaluations_total")


def record_robots_cache_hit() -> None:
    increment_counter("robots_cache_hits_total")


def record_robots_disallowed() -> None:
    increment_counter("robots_disallowed_total")

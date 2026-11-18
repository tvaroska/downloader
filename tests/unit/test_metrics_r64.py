"""Unit tests for Milestone 3 R64: metrics exposition, health score, and pool gauges."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from downloader.metrics import (
    MetricsCollector,
    record_html_rendering_detection,
    record_html_rendering_duration,
    record_html_rendering_failure,
    record_html_rendering_success,
)
from downloader.middleware import SystemMetricsCollector


@pytest.mark.unit
class TestPrometheusExpositionR64:
    """Verify Prometheus exposition fixes for R64."""

    def test_requests_total_not_doubled_by_status_series(self) -> None:
        collector = MetricsCollector()
        collector.record_request("/download", "GET", 200, 0.1)
        collector.record_request("/download", "GET", 200, 0.2)

        output = collector.get_prometheus_metrics()
        request_lines = [
            line for line in output.splitlines() if line.startswith("downloader_requests_total{")
        ]
        assert len(request_lines) == 1
        assert 'endpoint="GET_/download"' in request_lines[0]
        assert request_lines[0].endswith(" 2.0") or request_lines[0].endswith(" 2")
        assert 'endpoint="status_' not in output

        status_lines = [
            line
            for line in output.splitlines()
            if line.startswith("downloader_requests_by_status_total{")
        ]
        assert len(status_lines) == 1
        assert 'status="200"' in status_lines[0]

    def test_response_time_histogram_exports_sum_and_count(self) -> None:
        collector = MetricsCollector()
        collector.record_request("/render", "POST", 200, 0.25)
        collector.record_request("/render", "POST", 200, 0.75)

        output = collector.get_prometheus_metrics()
        assert 'downloader_response_time_seconds_sum{endpoint="POST_/render"} 1.0' in output
        assert 'downloader_response_time_seconds_count{endpoint="POST_/render"} 2' in output

    def test_html_rendering_metrics_are_exported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import downloader.metrics as metrics_mod

        collector = MetricsCollector()
        monkeypatch.setattr(metrics_mod, "_metrics_collector", collector)

        record_html_rendering_detection()
        record_html_rendering_duration(0.4)
        record_html_rendering_success(original_size=1000, rendered_size=2500)
        record_html_rendering_failure()

        output = collector.get_prometheus_metrics()
        assert "html_rendering_detections_total 1" in output
        assert "html_rendering_successes_total 1" in output
        assert "html_rendering_failures_total 1" in output
        assert 'html_rendering_duration_seconds_bucket{le="0.5"} 1' in output
        assert 'html_rendering_duration_seconds_bucket{le="+Inf"} 1' in output
        assert "html_rendering_duration_seconds_sum 0.4" in output
        assert "html_rendering_duration_seconds_count 1" in output
        assert 'downloader_gauge{name="html_rendering_latest_duration_seconds"} 0.4' in output
        assert 'downloader_gauge{name="html_rendering_latest_size_ratio"} 2.5' in output

    def test_health_score_exported_and_matches_grafana_dashboard(self) -> None:
        collector = MetricsCollector()
        collector.record_request("/download", "GET", 200, 0.1)
        output = collector.get_prometheus_metrics()
        assert "downloader_health_score 100" in output

        dashboard_path = (
            Path(__file__).resolve().parents[2] / "monitoring" / "grafana-dashboard.json"
        )
        dashboard = json.loads(dashboard_path.read_text(encoding="utf-8"))
        panel_expr = dashboard["dashboard"]["panels"][0]["targets"][0]["expr"]
        assert panel_expr == "downloader_health_score"


@pytest.mark.unit
class TestHealthScoreR64:
    """Verify health score 4xx exclusion and threshold 50."""

    def test_upstream_4xx_excluded_from_health_score_error_rate(self) -> None:
        collector = MetricsCollector()
        for _ in range(5):
            collector.record_request("/download", "GET", 200, 0.1)
        for _ in range(5):
            collector.record_request("/download", "GET", 404, 0.1)

        health = collector.get_system_health_score()
        assert health["overall_score"] == 100
        assert health["status"] == "healthy"
        assert "error_rate" not in health["factors"]

    def test_health_score_degraded_at_50_and_unhealthy_below_50(self) -> None:
        collector = MetricsCollector()
        # 10% 5xx -> 20 penalty; 3.5s avg response -> 25 penalty => score = 55 (>= 50, < 80 -> degraded)
        for _ in range(9):
            collector.record_request("/download", "GET", 200, 3.5)
        collector.record_request("/download", "GET", 500, 3.5)

        health = collector.get_system_health_score()
        assert 50 <= health["overall_score"] < 60
        assert health["status"] == "degraded"

        # 50% 5xx -> 30 penalty; 3.5s avg response -> 25 penalty => score = 45 (< 50 -> unhealthy)
        collector_bad = MetricsCollector()
        for _ in range(5):
            collector_bad.record_request("/download", "GET", 200, 3.5)
        for _ in range(5):
            collector_bad.record_request("/download", "GET", 500, 3.5)

        bad_health = collector_bad.get_system_health_score()
        assert bad_health["overall_score"] < 50
        assert bad_health["status"] == "unhealthy"


@pytest.mark.unit
class TestPdfPoolGaugeR64:
    """Verify pdf_pool_available reflects actual pool/semaphore availability."""

    @pytest.mark.asyncio
    async def test_pdf_pool_available_uses_pool_queue_or_semaphore(self) -> None:
        collector = SystemMetricsCollector()

        # 1. Pool queue present
        queue: asyncio.Queue[object] = asyncio.Queue(maxsize=3)
        await queue.put(object())
        await queue.put(object())
        pool = MagicMock()
        pool._available_browsers = queue
        pdf_gen = MagicMock()
        pdf_gen.pool = pool
        app_state = MagicMock()
        app_state.pdf_generator = pdf_gen
        collector.app_state = app_state

        await collector._collect_pdf_pool_metrics()
        assert collector.collector._gauges["pdf_pool_available"] == 2

        # 2. Lazy pool (pool is None), falls back to pdf_semaphore._value
        pdf_gen.pool = None
        app_state.pdf_semaphore = asyncio.Semaphore(4)
        await app_state.pdf_semaphore.acquire()
        await collector._collect_pdf_pool_metrics()
        assert collector.collector._gauges["pdf_pool_available"] == 3

        # 3. No pdf_generator -> 0
        app_state.pdf_generator = None
        await collector._collect_pdf_pool_metrics()
        assert collector.collector._gauges["pdf_pool_available"] == 0

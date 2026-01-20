"""Tests for metrics collection system."""

import time

from src.downloader.metrics import (
    MetricsCollector,
    MetricSnapshot,
    PerformanceMetrics,
    get_metrics_collector,
    increment_counter,
    record_html_rendering_cache_hit,
    record_html_rendering_detection,
    record_html_rendering_duration,
    record_html_rendering_failure,
    record_html_rendering_success,
    record_request,
    set_gauge,
)


class TestPerformanceMetrics:
    """Tests for PerformanceMetrics dataclass."""

    def test_avg_response_time_no_requests(self):
        """Test avg_response_time returns 0.0 when no requests."""
        perf = PerformanceMetrics()
        assert perf.avg_response_time == 0.0

    def test_avg_response_time_calculation(self):
        """Test avg_response_time calculates correctly."""
        perf = PerformanceMetrics(
            request_count=10,
            total_response_time=5.0,
        )
        assert perf.avg_response_time == 0.5

    def test_avg_response_time_single_request(self):
        """Test avg_response_time with single request."""
        perf = PerformanceMetrics(
            request_count=1,
            total_response_time=0.123,
        )
        assert perf.avg_response_time == 0.123

    def test_error_rate_no_requests(self):
        """Test error_rate returns 0.0 when no requests."""
        perf = PerformanceMetrics()
        assert perf.error_rate == 0.0

    def test_error_rate_no_errors(self):
        """Test error_rate returns 0.0 when no errors."""
        perf = PerformanceMetrics(
            request_count=100,
            error_count=0,
        )
        assert perf.error_rate == 0.0

    def test_error_rate_all_errors(self):
        """Test error_rate returns 100% when all requests are errors."""
        perf = PerformanceMetrics(
            request_count=50,
            error_count=50,
        )
        assert perf.error_rate == 100.0

    def test_error_rate_partial(self):
        """Test error_rate calculates partial error percentage."""
        perf = PerformanceMetrics(
            request_count=100,
            error_count=25,
        )
        assert perf.error_rate == 25.0

    def test_p95_response_time_empty(self):
        """Test p95_response_time returns 0.0 when no data."""
        perf = PerformanceMetrics()
        assert perf.p95_response_time == 0.0

    def test_p95_response_time_single(self):
        """Test p95_response_time with single value returns that value."""
        perf = PerformanceMetrics()
        perf.response_times.append(0.5)
        assert perf.p95_response_time == 0.5

    def test_p95_response_time_calculation(self):
        """Test p95_response_time calculates correct percentile."""
        perf = PerformanceMetrics()
        # Add 100 response times: 0.01, 0.02, ..., 1.00
        for i in range(1, 101):
            perf.response_times.append(i * 0.01)
        # P95 should be around 0.95-0.96
        assert 0.95 <= perf.p95_response_time <= 0.96

    def test_p95_response_time_boundary(self):
        """Test p95_response_time handles index boundary correctly."""
        perf = PerformanceMetrics()
        # Exact 20 values - p95 index = int(20 * 0.95) = 19
        for i in range(1, 21):
            perf.response_times.append(float(i))
        # Index 19 should be value 20 (0-indexed: 19th position is the 20th value)
        assert perf.p95_response_time == 20.0

    def test_default_deque_maxlen(self):
        """Test response_times deque has correct maxlen."""
        perf = PerformanceMetrics()
        # Default maxlen should be 1000
        for i in range(1500):
            perf.response_times.append(float(i))
        assert len(perf.response_times) == 1000


class TestMetricsCollector:
    """Tests for MetricsCollector class."""

    def test_init_default_history_size(self):
        """Test default history size."""
        collector = MetricsCollector()
        assert collector.history_size == 10000

    def test_init_custom_history_size(self):
        """Test custom history size."""
        collector = MetricsCollector(history_size=5000)
        assert collector.history_size == 5000

    def test_record_request_success_200(self):
        """Test recording a successful 200 request."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)

        key = "GET_/test"
        perf = collector._performance_metrics[key]
        assert perf.request_count == 1
        assert perf.error_count == 0
        assert perf.total_response_time == 0.1

    def test_record_request_error_4xx(self):
        """Test recording a 4xx error request."""
        collector = MetricsCollector()
        collector.record_request("/test", "POST", 400, 0.05)

        key = "POST_/test"
        perf = collector._performance_metrics[key]
        assert perf.request_count == 1
        assert perf.error_count == 1

    def test_record_request_error_5xx(self):
        """Test recording a 5xx error request."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 500, 0.2)

        key = "GET_/test"
        perf = collector._performance_metrics[key]
        assert perf.request_count == 1
        assert perf.error_count == 1

    def test_record_request_updates_min_max(self):
        """Test min/max response times are updated correctly."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.5)
        collector.record_request("/test", "GET", 200, 0.1)
        collector.record_request("/test", "GET", 200, 0.9)

        key = "GET_/test"
        perf = collector._performance_metrics[key]
        assert perf.min_response_time == 0.1
        assert perf.max_response_time == 0.9

    def test_record_request_appends_to_deque(self):
        """Test response times are appended to deque."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)
        collector.record_request("/test", "GET", 200, 0.2)

        key = "GET_/test"
        perf = collector._performance_metrics[key]
        assert len(perf.response_times) == 2
        assert list(perf.response_times) == [0.1, 0.2]

    def test_record_request_histogram_buckets(self):
        """Test histogram buckets are incremented correctly."""
        collector = MetricsCollector()
        # Response time 0.05 should hit buckets 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, inf
        collector.record_request("/test", "GET", 200, 0.05)

        key = "response_time_GET_/test"
        assert collector._histograms[key][0.1] == 1
        assert collector._histograms[key][0.25] == 1
        assert collector._histograms[key][float("inf")] == 1

    def test_record_request_histogram_large_value(self):
        """Test histogram with large response time."""
        collector = MetricsCollector()
        # Response time 15.0 should only hit inf bucket
        collector.record_request("/test", "GET", 200, 15.0)

        key = "response_time_GET_/test"
        assert collector._histograms[key][0.1] == 0
        assert collector._histograms[key][10.0] == 0
        assert collector._histograms[key][float("inf")] == 1

    def test_record_request_counter_updates(self):
        """Test request counters are updated."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)
        collector.record_request("/test", "GET", 500, 0.2)

        assert collector._counters["requests_total_GET_/test"] == 2
        assert collector._counters["requests_total_status_200"] == 1
        assert collector._counters["requests_total_status_500"] == 1
        assert collector._counters["errors_total_GET_/test"] == 1

    def test_record_request_stores_in_history(self):
        """Test request is stored in history."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)

        history_key = "response_time_GET_/test"
        assert len(collector._metrics_history[history_key]) == 1
        snapshot = collector._metrics_history[history_key][0]
        assert snapshot.value == 0.1
        assert snapshot.labels["method"] == "GET"
        assert snapshot.labels["endpoint"] == "/test"
        assert snapshot.labels["status"] == "200"

    def test_set_gauge_basic(self):
        """Test setting a gauge value."""
        collector = MetricsCollector()
        collector.set_gauge("test_gauge", 42.5)
        assert collector._gauges["test_gauge"] == 42.5

    def test_set_gauge_overwrites(self):
        """Test setting gauge overwrites previous value."""
        collector = MetricsCollector()
        collector.set_gauge("test_gauge", 10.0)
        collector.set_gauge("test_gauge", 20.0)
        assert collector._gauges["test_gauge"] == 20.0

    def test_set_gauge_history(self):
        """Test gauge is recorded in history."""
        collector = MetricsCollector()
        collector.set_gauge("test_gauge", 42.5)

        assert len(collector._metrics_history["test_gauge"]) == 1
        snapshot = collector._metrics_history["test_gauge"][0]
        assert snapshot.value == 42.5

    def test_increment_counter_default(self):
        """Test increment counter by default value (1)."""
        collector = MetricsCollector()
        collector.increment_counter("test_counter")
        assert collector._counters["test_counter"] == 1.0

    def test_increment_counter_custom(self):
        """Test increment counter by custom value."""
        collector = MetricsCollector()
        collector.increment_counter("test_counter", 5.0)
        assert collector._counters["test_counter"] == 5.0

    def test_increment_counter_accumulates(self):
        """Test counter accumulates over multiple increments."""
        collector = MetricsCollector()
        collector.increment_counter("test_counter", 2.0)
        collector.increment_counter("test_counter", 3.0)
        collector.increment_counter("test_counter")
        assert collector._counters["test_counter"] == 6.0


class TestMetricsCollectorSummary:
    """Tests for MetricsCollector performance summary methods."""

    def test_get_performance_summary_empty(self):
        """Test performance summary with no requests."""
        collector = MetricsCollector()
        summary = collector.get_performance_summary()

        assert "uptime_seconds" in summary
        assert summary["total_requests"] == 0
        assert summary["total_errors"] == 0
        assert summary["endpoints"] == {}
        assert summary["overall_error_rate_percent"] == 0.0
        assert summary["overall_avg_response_time"] == 0.0

    def test_get_performance_summary_single_endpoint(self):
        """Test performance summary with single endpoint."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)
        collector.record_request("/test", "GET", 200, 0.2)
        collector.record_request("/test", "GET", 500, 0.3)

        summary = collector.get_performance_summary()

        assert summary["total_requests"] == 3
        assert summary["total_errors"] == 1
        assert "GET_/test" in summary["endpoints"]

        endpoint = summary["endpoints"]["GET_/test"]
        assert endpoint["request_count"] == 3
        assert endpoint["error_count"] == 1
        assert endpoint["min_response_time"] == 0.1
        assert endpoint["max_response_time"] == 0.3

    def test_get_performance_summary_multiple_endpoints(self):
        """Test performance summary with multiple endpoints."""
        collector = MetricsCollector()
        collector.record_request("/api/a", "GET", 200, 0.1)
        collector.record_request("/api/b", "POST", 201, 0.2)

        summary = collector.get_performance_summary()

        assert summary["total_requests"] == 2
        assert len(summary["endpoints"]) == 2
        assert "GET_/api/a" in summary["endpoints"]
        assert "POST_/api/b" in summary["endpoints"]

    def test_get_performance_summary_min_inf_handling(self):
        """Test min_response_time inf is converted to 0."""
        collector = MetricsCollector()
        # Don't record any requests - min will be inf
        perf = PerformanceMetrics()
        collector._performance_metrics["GET_/test"] = perf

        summary = collector.get_performance_summary()

        endpoint = summary["endpoints"]["GET_/test"]
        assert endpoint["min_response_time"] == 0

    def test_get_performance_summary_overall_error_rate(self):
        """Test overall error rate calculation."""
        collector = MetricsCollector()
        collector.record_request("/a", "GET", 200, 0.1)
        collector.record_request("/a", "GET", 200, 0.1)
        collector.record_request("/a", "GET", 500, 0.1)
        collector.record_request("/a", "GET", 404, 0.1)

        summary = collector.get_performance_summary()

        # 2 errors out of 4 = 50%
        assert summary["overall_error_rate_percent"] == 50.0

    def test_get_performance_summary_overall_avg_response_time(self):
        """Test overall average response time calculation."""
        collector = MetricsCollector()
        collector.record_request("/a", "GET", 200, 0.1)
        collector.record_request("/a", "GET", 200, 0.2)
        collector.record_request("/b", "GET", 200, 0.3)

        summary = collector.get_performance_summary()

        # (0.1 + 0.2 + 0.3) / 3 = 0.2
        assert abs(summary["overall_avg_response_time"] - 0.2) < 0.001


class TestMetricsCollectorPrometheus:
    """Tests for Prometheus metrics generation."""

    def test_get_prometheus_metrics_format(self):
        """Test Prometheus output format."""
        collector = MetricsCollector()
        output = collector.get_prometheus_metrics()

        assert "# HELP downloader_uptime_seconds" in output
        assert "# TYPE downloader_uptime_seconds gauge" in output
        assert "downloader_uptime_seconds" in output

    def test_get_prometheus_metrics_with_requests(self):
        """Test Prometheus output with recorded requests."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)

        output = collector.get_prometheus_metrics()

        assert "downloader_requests_total" in output
        assert 'endpoint="GET_/test"' in output

    def test_get_prometheus_metrics_with_errors(self):
        """Test Prometheus output includes errors."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 500, 0.1)

        output = collector.get_prometheus_metrics()

        assert "downloader_errors_total" in output

    def test_get_prometheus_metrics_with_gauges(self):
        """Test Prometheus output includes gauges."""
        collector = MetricsCollector()
        collector.set_gauge("test_gauge", 42.0)

        output = collector.get_prometheus_metrics()

        assert "downloader_gauge" in output
        assert 'name="test_gauge"' in output
        assert "42" in output

    def test_get_prometheus_metrics_histogram_inf_bucket(self):
        """Test Prometheus histogram uses +Inf notation."""
        collector = MetricsCollector()
        collector.record_request("/test", "GET", 200, 0.1)

        output = collector.get_prometheus_metrics()

        assert 'le="+Inf"' in output


class TestMetricsCollectorRecentMetrics:
    """Tests for recent metrics retrieval."""

    def test_get_recent_metrics_empty(self):
        """Test get_recent_metrics returns empty list for non-existent metric."""
        collector = MetricsCollector()
        result = collector.get_recent_metrics("nonexistent", seconds=300)
        assert result == []

    def test_get_recent_metrics_within_window(self):
        """Test get_recent_metrics returns metrics within time window."""
        collector = MetricsCollector()
        collector.set_gauge("test", 1.0)
        collector.set_gauge("test", 2.0)

        result = collector.get_recent_metrics("test", seconds=300)

        assert len(result) == 2

    def test_get_recent_metrics_outside_window(self):
        """Test get_recent_metrics excludes metrics outside time window."""
        collector = MetricsCollector()

        # Add an old metric by manipulating history directly
        old_snapshot = MetricSnapshot(
            timestamp=time.time() - 600,  # 10 minutes ago
            value=1.0,
        )
        collector._metrics_history["test"].append(old_snapshot)

        # Add a recent metric
        collector.set_gauge("test", 2.0)

        # Only get last 5 minutes
        result = collector.get_recent_metrics("test", seconds=300)

        assert len(result) == 1
        assert result[0].value == 2.0


class TestHealthScore:
    """Tests for system health score calculation."""

    def test_health_score_all_healthy(self):
        """Test perfect health score when no issues."""
        collector = MetricsCollector()
        # Record some successful requests
        for _ in range(10):
            collector.record_request("/test", "GET", 200, 0.1)

        health = collector.get_system_health_score()

        assert health["overall_score"] == 100
        assert health["status"] == "healthy"

    def test_health_score_error_penalty(self):
        """Test error rate penalty when > 5%."""
        collector = MetricsCollector()
        # 10% error rate (10 errors out of 100)
        for _ in range(90):
            collector.record_request("/test", "GET", 200, 0.1)
        for _ in range(10):
            collector.record_request("/test", "GET", 500, 0.1)

        health = collector.get_system_health_score()

        assert "error_rate" in health["factors"]
        assert health["factors"]["error_rate"]["status"] == "warning"
        assert health["overall_score"] < 100

    def test_health_score_error_critical(self):
        """Test critical status when error rate > 10%."""
        collector = MetricsCollector()
        # 15% error rate
        for _ in range(85):
            collector.record_request("/test", "GET", 200, 0.1)
        for _ in range(15):
            collector.record_request("/test", "GET", 500, 0.1)

        health = collector.get_system_health_score()

        assert health["factors"]["error_rate"]["status"] == "critical"

    def test_health_score_response_time_penalty(self):
        """Test response time penalty when > 1.0s."""
        collector = MetricsCollector()
        # Average response time > 1.0s
        for _ in range(10):
            collector.record_request("/test", "GET", 200, 2.0)

        health = collector.get_system_health_score()

        assert "response_time" in health["factors"]
        assert health["factors"]["response_time"]["status"] == "warning"
        assert health["overall_score"] < 100

    def test_health_score_response_time_critical(self):
        """Test critical status when response time > 3.0s."""
        collector = MetricsCollector()
        for _ in range(10):
            collector.record_request("/test", "GET", 200, 4.0)

        health = collector.get_system_health_score()

        assert health["factors"]["response_time"]["status"] == "critical"

    def test_health_score_degraded(self):
        """Test degraded status when score 60-79."""
        collector = MetricsCollector()
        # Create conditions for ~70 score
        # Error penalty: 10% = 10 points
        # Response penalty: 2s = 10 points
        for _ in range(90):
            collector.record_request("/test", "GET", 200, 2.0)
        for _ in range(10):
            collector.record_request("/test", "GET", 500, 2.0)

        health = collector.get_system_health_score()

        assert 60 <= health["overall_score"] < 80
        assert health["status"] == "degraded"

    def test_health_score_unhealthy(self):
        """Test unhealthy status when score < 60."""
        collector = MetricsCollector()
        # High error rate + slow response
        for _ in range(50):
            collector.record_request("/test", "GET", 200, 5.0)
        for _ in range(50):
            collector.record_request("/test", "GET", 500, 5.0)

        health = collector.get_system_health_score()

        assert health["overall_score"] < 60
        assert health["status"] == "unhealthy"

    def test_health_score_bounds(self):
        """Test health score stays within 0-100 bounds."""
        collector = MetricsCollector()
        # Extreme conditions that could push below 0
        for _ in range(100):
            collector.record_request("/test", "GET", 500, 10.0)

        health = collector.get_system_health_score()

        assert health["overall_score"] >= 0
        assert health["overall_score"] <= 100

    def test_health_score_uptime_factor(self):
        """Test uptime is included in health factors after 24h."""
        collector = MetricsCollector()
        # Simulate 24+ hours of uptime
        collector._start_time = time.time() - 90000  # 25 hours ago

        for _ in range(10):
            collector.record_request("/test", "GET", 200, 0.1)

        health = collector.get_system_health_score()

        assert "uptime" in health["factors"]
        assert health["factors"]["uptime"]["status"] == "good"


class TestModuleFunctions:
    """Tests for module-level convenience functions."""

    def test_get_metrics_collector_singleton(self):
        """Test get_metrics_collector returns same instance."""
        collector1 = get_metrics_collector()
        collector2 = get_metrics_collector()
        assert collector1 is collector2

    def test_record_request_wrapper(self):
        """Test record_request module function delegates to collector."""
        # Reset global collector
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_request("/test", "GET", 200, 0.1)

        collector = get_metrics_collector()
        assert collector._counters["requests_total_GET_/test"] == 1

    def test_set_gauge_wrapper(self):
        """Test set_gauge module function delegates to collector."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        set_gauge("test_gauge", 42.0)

        collector = get_metrics_collector()
        assert collector._gauges["test_gauge"] == 42.0

    def test_increment_counter_wrapper(self):
        """Test increment_counter module function delegates to collector."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        increment_counter("test_counter", 5.0)

        collector = get_metrics_collector()
        assert collector._counters["test_counter"] == 5.0


class TestHTMLRenderingMetrics:
    """Tests for HTML rendering metric functions."""

    def test_record_html_rendering_detection(self):
        """Test record_html_rendering_detection increments counter."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_detection()

        collector = get_metrics_collector()
        assert collector._counters["html_rendering_detections_total"] == 1

    def test_record_html_rendering_cache_hit(self):
        """Test record_html_rendering_cache_hit increments counter."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_cache_hit()

        collector = get_metrics_collector()
        assert collector._counters["html_rendering_cache_hits_total"] == 1

    def test_record_html_rendering_duration(self):
        """Test record_html_rendering_duration updates histogram and gauge."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_duration(0.5)

        collector = get_metrics_collector()
        assert collector._gauges["html_rendering_latest_duration_seconds"] == 0.5
        assert collector._histograms["html_rendering_duration_seconds"][0.5] == 1

    def test_record_html_rendering_failure(self):
        """Test record_html_rendering_failure increments counter."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_failure()

        collector = get_metrics_collector()
        assert collector._counters["html_rendering_failures_total"] == 1

    def test_record_html_rendering_success(self):
        """Test record_html_rendering_success increments counter and sets ratio."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_success(original_size=1000, rendered_size=2000)

        collector = get_metrics_collector()
        assert collector._counters["html_rendering_successes_total"] == 1
        assert collector._gauges["html_rendering_latest_size_ratio"] == 2.0

    def test_record_html_rendering_success_zero_original(self):
        """Test record_html_rendering_success handles zero original size."""
        import src.downloader.metrics as metrics_module

        metrics_module._metrics_collector = MetricsCollector()

        record_html_rendering_success(original_size=0, rendered_size=1000)

        collector = get_metrics_collector()
        assert collector._counters["html_rendering_successes_total"] == 1
        # Ratio not set when original_size is 0
        assert "html_rendering_latest_size_ratio" not in collector._gauges

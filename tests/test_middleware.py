"""Tests for middleware components."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.downloader.middleware import (
    MetricsMiddleware,
    SystemMetricsCollector,
    get_system_metrics_collector,
)


class TestMetricsMiddlewareNormalizePath:
    """Tests for MetricsMiddleware._normalize_path."""

    def test_normalize_path_batch(self):
        """Test /batch/* normalizes to /batch."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/batch") == "/batch"
        assert middleware._normalize_path("/batch/123") == "/batch"
        assert middleware._normalize_path("/batch/status") == "/batch"

    def test_normalize_path_health(self):
        """Test /health normalizes to /health."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/health") == "/health"

    def test_normalize_path_metrics(self):
        """Test /metrics normalizes to /metrics."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/metrics") == "/metrics"

    def test_normalize_path_jobs(self):
        """Test /jobs/* normalizes to /jobs/{job_id}."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/jobs/123") == "/jobs/{job_id}"
        assert middleware._normalize_path("/jobs/abc-def-456") == "/jobs/{job_id}"

    def test_normalize_path_status(self):
        """Test /status/* normalizes to /status/{job_id}."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/status/123") == "/status/{job_id}"
        assert middleware._normalize_path("/status/uuid-here") == "/status/{job_id}"

    def test_normalize_path_results(self):
        """Test /results/* normalizes to /results/{job_id}."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/results/123") == "/results/{job_id}"

    def test_normalize_path_root(self):
        """Test / normalizes to /."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/") == "/"

    def test_normalize_path_unknown(self):
        """Test unknown paths normalize to /download."""
        middleware = MetricsMiddleware(app=MagicMock())
        assert middleware._normalize_path("/foo") == "/download"
        assert middleware._normalize_path("/custom/path") == "/download"
        assert middleware._normalize_path("/https://example.com") == "/download"


class TestMetricsMiddlewareDispatch:
    """Tests for MetricsMiddleware.dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_successful_request(self):
        """Test dispatch records metrics for successful request."""
        middleware = MetricsMiddleware(app=MagicMock())

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/health"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        with patch("src.downloader.middleware.get_metrics_collector") as mock_get_collector:
            mock_collector = MagicMock()
            mock_get_collector.return_value = mock_collector

            await middleware.dispatch(mock_request, mock_call_next)

            mock_collector.record_request.assert_called_once()
            call_kwargs = mock_collector.record_request.call_args[1]
            assert call_kwargs["endpoint"] == "/health"
            assert call_kwargs["method"] == "GET"
            assert call_kwargs["status_code"] == 200
            assert "response_time" in call_kwargs

    @pytest.mark.asyncio
    async def test_dispatch_error_response(self):
        """Test dispatch records metrics for error response."""
        middleware = MetricsMiddleware(app=MagicMock())

        mock_request = MagicMock()
        mock_request.method = "POST"
        mock_request.url.path = "/batch"

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        with patch("src.downloader.middleware.get_metrics_collector") as mock_get_collector:
            mock_collector = MagicMock()
            mock_get_collector.return_value = mock_collector

            await middleware.dispatch(mock_request, mock_call_next)

            call_kwargs = mock_collector.record_request.call_args[1]
            assert call_kwargs["status_code"] == 500

    @pytest.mark.asyncio
    async def test_dispatch_exception_path(self):
        """Test dispatch records metrics when exception occurs."""
        middleware = MetricsMiddleware(app=MagicMock())

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/test"

        async def mock_call_next(request):
            raise ValueError("Test error")

        with patch("src.downloader.middleware.get_metrics_collector") as mock_get_collector:
            mock_collector = MagicMock()
            mock_get_collector.return_value = mock_collector

            with pytest.raises(ValueError):
                await middleware.dispatch(mock_request, mock_call_next)

            # Should still record metrics with 500 status
            call_kwargs = mock_collector.record_request.call_args[1]
            assert call_kwargs["status_code"] == 500

    @pytest.mark.asyncio
    async def test_dispatch_adds_response_time_header(self):
        """Test dispatch adds X-Response-Time header."""
        middleware = MetricsMiddleware(app=MagicMock())

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/health"

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {}

        async def mock_call_next(request):
            return mock_response

        with patch("src.downloader.middleware.get_metrics_collector"):
            response = await middleware.dispatch(mock_request, mock_call_next)

            assert "X-Response-Time" in response.headers
            assert response.headers["X-Response-Time"].endswith("s")

    @pytest.mark.asyncio
    async def test_dispatch_exception_reraised(self):
        """Test dispatch re-raises exceptions after recording metrics."""
        middleware = MetricsMiddleware(app=MagicMock())

        mock_request = MagicMock()
        mock_request.method = "GET"
        mock_request.url.path = "/test"

        async def mock_call_next(request):
            raise RuntimeError("Something went wrong")

        with patch("src.downloader.middleware.get_metrics_collector"):
            with pytest.raises(RuntimeError, match="Something went wrong"):
                await middleware.dispatch(mock_request, mock_call_next)


class TestSystemMetricsCollectorInit:
    """Tests for SystemMetricsCollector initialization."""

    def test_init_creates_collector(self):
        """Test init creates a metrics collector reference."""
        collector = SystemMetricsCollector()
        assert collector.collector is not None
        assert collector._running is False
        assert collector._task is None
        assert collector.app_state is None


class TestSystemMetricsCollectorStart:
    """Tests for SystemMetricsCollector.start."""

    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        """Test start creates a background task."""
        collector = SystemMetricsCollector()

        with patch.object(collector, "_collect_system_metrics", new_callable=AsyncMock):
            await collector.start()

            assert collector._running is True
            assert collector._task is not None

            await collector.stop()

    @pytest.mark.asyncio
    async def test_start_stores_app_state(self):
        """Test start stores the app_state parameter."""
        collector = SystemMetricsCollector()

        mock_app_state = MagicMock()

        with patch.object(collector, "_collect_system_metrics", new_callable=AsyncMock):
            await collector.start(mock_app_state)

            assert collector.app_state is mock_app_state

            await collector.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        """Test start is idempotent - only creates one task."""
        collector = SystemMetricsCollector()

        with patch.object(collector, "_collect_system_metrics", new_callable=AsyncMock):
            await collector.start()
            first_task = collector._task

            await collector.start()  # Call again
            second_task = collector._task

            assert first_task is second_task

            await collector.stop()


class TestSystemMetricsCollectorStop:
    """Tests for SystemMetricsCollector.stop."""

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self):
        """Test stop sets _running to False."""
        collector = SystemMetricsCollector()

        with patch.object(collector, "_collect_system_metrics", new_callable=AsyncMock):
            await collector.start()
            assert collector._running is True

            await collector.stop()
            assert collector._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        """Test stop cancels the background task."""
        collector = SystemMetricsCollector()

        with patch.object(collector, "_collect_system_metrics", new_callable=AsyncMock):
            await collector.start()
            task = collector._task

            await collector.stop()

            assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_handles_no_task(self):
        """Test stop handles case when no task exists."""
        collector = SystemMetricsCollector()

        # Should not raise
        await collector.stop()

        assert collector._running is False


class TestSystemMetricsCollectorCollect:
    """Tests for SystemMetricsCollector metric collection."""

    @pytest.mark.asyncio
    async def test_collect_metrics_snapshot_with_semaphores(self):
        """Test _collect_metrics_snapshot collects semaphore utilization."""
        collector = SystemMetricsCollector()

        # Create mock app_state with semaphores
        mock_app_state = MagicMock()
        mock_pdf_semaphore = MagicMock()
        mock_pdf_semaphore._value = 8  # 4 in use (12 - 8)
        mock_batch_semaphore = MagicMock()
        mock_batch_semaphore._value = 40  # 10 in use (50 - 40)

        mock_settings = MagicMock()
        mock_settings.pdf.concurrency = 12
        mock_settings.batch.concurrency = 50

        mock_app_state.pdf_semaphore = mock_pdf_semaphore
        mock_app_state.batch_semaphore = mock_batch_semaphore
        mock_app_state.settings = mock_settings

        collector.app_state = mock_app_state

        with patch.object(collector, "_collect_redis_metrics", new_callable=AsyncMock):
            with patch.object(collector, "_collect_pdf_pool_metrics", new_callable=AsyncMock):
                with patch.object(
                    collector, "_collect_http_client_metrics", new_callable=AsyncMock
                ):
                    mock_collector = MagicMock()
                    collector.collector = mock_collector

                    await collector._collect_metrics_snapshot()

                    # Check that gauges were set
                    set_gauge_calls = mock_collector.set_gauge.call_args_list
                    gauge_names = [call[0][0] for call in set_gauge_calls]
                    assert "pdf_concurrency_utilization" in gauge_names
                    assert "batch_concurrency_utilization" in gauge_names

    @pytest.mark.asyncio
    async def test_collect_metrics_snapshot_without_app_state(self):
        """Test _collect_metrics_snapshot handles missing app_state."""
        collector = SystemMetricsCollector()
        collector.app_state = None

        with patch.object(collector, "_collect_redis_metrics", new_callable=AsyncMock):
            with patch.object(collector, "_collect_pdf_pool_metrics", new_callable=AsyncMock):
                with patch.object(
                    collector, "_collect_http_client_metrics", new_callable=AsyncMock
                ):
                    # Should not raise
                    await collector._collect_metrics_snapshot()

    @pytest.mark.asyncio
    async def test_collect_redis_metrics_healthy(self):
        """Test _collect_redis_metrics sets redis_status=1 when healthy."""
        collector = SystemMetricsCollector()

        mock_job_manager = AsyncMock()
        mock_job_manager.get_connection_stats = AsyncMock(
            return_value={
                "status": "healthy",
                "created_connections": 5,
                "available_connections": 3,
                "in_use_connections": 2,
            }
        )

        mock_app_state = MagicMock()
        mock_app_state.job_manager = mock_job_manager
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        await collector._collect_redis_metrics()

        # Check redis_status was set to 1
        set_gauge_calls = mock_collector.set_gauge.call_args_list
        redis_status_call = [c for c in set_gauge_calls if c[0][0] == "redis_status"]
        assert len(redis_status_call) == 1
        assert redis_status_call[0][0][1] == 1

    @pytest.mark.asyncio
    async def test_collect_redis_metrics_unhealthy(self):
        """Test _collect_redis_metrics sets redis_status=0 when unhealthy."""
        collector = SystemMetricsCollector()

        mock_job_manager = AsyncMock()
        mock_job_manager.get_connection_stats = AsyncMock(return_value={"status": "unhealthy"})

        mock_app_state = MagicMock()
        mock_app_state.job_manager = mock_job_manager
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        await collector._collect_redis_metrics()

        set_gauge_calls = mock_collector.set_gauge.call_args_list
        redis_status_call = [c for c in set_gauge_calls if c[0][0] == "redis_status"]
        assert len(redis_status_call) == 1
        assert redis_status_call[0][0][1] == 0

    @pytest.mark.asyncio
    async def test_collect_redis_metrics_exception(self):
        """Test _collect_redis_metrics sets redis_status=0 on exception."""
        collector = SystemMetricsCollector()

        mock_job_manager = AsyncMock()
        mock_job_manager.get_connection_stats = AsyncMock(side_effect=Exception("Redis error"))

        mock_app_state = MagicMock()
        mock_app_state.job_manager = mock_job_manager
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        # Should not raise
        await collector._collect_redis_metrics()

        set_gauge_calls = mock_collector.set_gauge.call_args_list
        redis_status_call = [c for c in set_gauge_calls if c[0][0] == "redis_status"]
        assert len(redis_status_call) == 1
        assert redis_status_call[0][0][1] == 0

    @pytest.mark.asyncio
    async def test_collect_http_client_metrics_healthy(self):
        """Test _collect_http_client_metrics sets http_client_status=1 when healthy."""
        collector = SystemMetricsCollector()

        mock_http_client = MagicMock()
        mock_http_client.get_connection_stats = MagicMock(
            return_value={
                "status": "healthy",
                "circuit_breakers": {"example.com": {"state": "closed", "failure_count": 0}},
            }
        )

        mock_app_state = MagicMock()
        mock_app_state.http_client = mock_http_client
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        await collector._collect_http_client_metrics()

        set_gauge_calls = mock_collector.set_gauge.call_args_list
        http_status_call = [c for c in set_gauge_calls if c[0][0] == "http_client_status"]
        assert len(http_status_call) == 1
        assert http_status_call[0][0][1] == 1

    @pytest.mark.asyncio
    async def test_collect_http_client_metrics_circuit_breaker(self):
        """Test _collect_http_client_metrics extracts circuit breaker stats."""
        collector = SystemMetricsCollector()

        mock_http_client = MagicMock()
        mock_http_client.get_connection_stats = MagicMock(
            return_value={
                "status": "healthy",
                "circuit_breakers": {"example.com": {"state": "closed", "failure_count": 3}},
            }
        )

        mock_app_state = MagicMock()
        mock_app_state.http_client = mock_http_client
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        await collector._collect_http_client_metrics()

        set_gauge_calls = mock_collector.set_gauge.call_args_list
        gauge_names = [c[0][0] for c in set_gauge_calls]

        # URL dots should be normalized to underscores
        assert "circuit_breaker_example_com_failures" in gauge_names
        assert "circuit_breaker_example_com_state" in gauge_names

    @pytest.mark.asyncio
    async def test_collect_http_client_metrics_exception(self):
        """Test _collect_http_client_metrics sets http_client_status=0 on exception."""
        collector = SystemMetricsCollector()

        mock_http_client = MagicMock()
        mock_http_client.get_connection_stats = MagicMock(
            side_effect=Exception("HTTP client error")
        )

        mock_app_state = MagicMock()
        mock_app_state.http_client = mock_http_client
        collector.app_state = mock_app_state

        mock_collector = MagicMock()
        collector.collector = mock_collector

        # Should not raise
        await collector._collect_http_client_metrics()

        set_gauge_calls = mock_collector.set_gauge.call_args_list
        http_status_call = [c for c in set_gauge_calls if c[0][0] == "http_client_status"]
        assert len(http_status_call) == 1
        assert http_status_call[0][0][1] == 0


class TestGetSystemMetricsCollector:
    """Tests for get_system_metrics_collector function."""

    def test_get_system_metrics_collector_singleton(self):
        """Test get_system_metrics_collector returns singleton."""
        # Reset global
        import src.downloader.middleware as middleware_module

        middleware_module._system_metrics_collector = None

        collector1 = get_system_metrics_collector()
        collector2 = get_system_metrics_collector()

        assert collector1 is collector2

    def test_get_system_metrics_collector_creates_instance(self):
        """Test get_system_metrics_collector creates instance if none exists."""
        import src.downloader.middleware as middleware_module

        middleware_module._system_metrics_collector = None

        collector = get_system_metrics_collector()

        assert collector is not None
        assert isinstance(collector, SystemMetricsCollector)

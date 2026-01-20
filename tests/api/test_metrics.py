"""Integration tests for metrics endpoints."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from src.downloader.dependencies import get_batch_semaphore, get_pdf_semaphore
from src.downloader.main import app


@pytest.fixture
def mock_pdf_semaphore():
    """Mock PDF semaphore with 4 of 12 in use."""
    semaphore = MagicMock()
    semaphore._value = 8  # 4 in use (12 - 8)
    return semaphore


@pytest.fixture
def mock_batch_semaphore():
    """Mock batch semaphore with 10 of 50 in use."""
    semaphore = MagicMock()
    semaphore._value = 40  # 10 in use (50 - 40)
    return semaphore


@pytest.fixture
def mock_job_manager():
    """Mock job manager with healthy Redis connection."""
    manager = AsyncMock()
    manager.get_connection_stats = AsyncMock(
        return_value={
            "status": "healthy",
            "created_connections": 5,
            "available_connections": 3,
            "in_use_connections": 2,
        }
    )
    return manager


@pytest.fixture
def mock_http_client():
    """Mock HTTP client with healthy status."""
    client = MagicMock()
    client.get_connection_stats = MagicMock(
        return_value={
            "status": "healthy",
            "circuit_breakers": {
                "example.com": {"state": "closed", "failure_count": 0},
            },
        }
    )
    return client


@pytest.fixture
async def api_client(mock_pdf_semaphore, mock_batch_semaphore, mock_job_manager, mock_http_client):
    """Create async test client with mocked dependencies."""

    async def mock_get_pdf_semaphore():
        return mock_pdf_semaphore

    async def mock_get_batch_semaphore():
        return mock_batch_semaphore

    app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
    app.dependency_overrides[get_batch_semaphore] = mock_get_batch_semaphore

    # Mock app.state
    app.state.job_manager = mock_job_manager
    app.state.http_client = mock_http_client

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.pop(get_pdf_semaphore, None)
    app.dependency_overrides.pop(get_batch_semaphore, None)


@pytest.mark.asyncio
class TestMetricsEndpoint:
    """Tests for GET /metrics endpoint."""

    async def test_get_metrics_returns_200(self, api_client):
        """Test metrics endpoint returns 200 status."""
        response = await api_client.get("/metrics")
        assert response.status_code == 200

    async def test_get_metrics_prometheus_format(self, api_client):
        """Test metrics endpoint returns Prometheus text format."""
        response = await api_client.get("/metrics")
        content_type = response.headers.get("content-type", "")
        assert "text/plain" in content_type

    async def test_get_metrics_contains_sections(self, api_client):
        """Test metrics output contains expected sections."""
        response = await api_client.get("/metrics")
        content = response.text

        assert "downloader_uptime_seconds" in content
        assert "# HELP" in content
        assert "# TYPE" in content


@pytest.mark.asyncio
class TestPerformanceMetricsEndpoint:
    """Tests for GET /metrics/performance endpoint."""

    async def test_get_performance_metrics_returns_200(self, api_client):
        """Test performance metrics endpoint returns 200 status."""
        response = await api_client.get("/metrics/performance")
        assert response.status_code == 200

    async def test_get_performance_metrics_json_structure(self, api_client):
        """Test performance metrics returns expected JSON structure."""
        response = await api_client.get("/metrics/performance")
        data = response.json()

        assert "uptime_seconds" in data
        assert "total_requests" in data
        assert "total_errors" in data
        assert "endpoints" in data
        assert "overall_error_rate_percent" in data
        assert "overall_avg_response_time" in data


@pytest.mark.asyncio
class TestHealthScoreEndpoint:
    """Tests for GET /metrics/health-score endpoint."""

    async def test_get_health_score_returns_200(self, api_client):
        """Test health score endpoint returns 200 status."""
        response = await api_client.get("/metrics/health-score")
        assert response.status_code == 200

    async def test_get_health_score_structure(self, api_client):
        """Test health score returns expected structure."""
        response = await api_client.get("/metrics/health-score")
        data = response.json()

        assert "overall_score" in data
        assert "status" in data
        assert "factors" in data


@pytest.mark.asyncio
class TestLiveMetricsEndpoint:
    """Tests for GET /metrics/live endpoint."""

    async def test_get_live_metrics_returns_200(self, api_client):
        """Test live metrics endpoint returns 200 status."""
        response = await api_client.get("/metrics/live")
        assert response.status_code == 200

    async def test_get_live_metrics_structure(self, api_client):
        """Test live metrics returns expected structure."""
        response = await api_client.get("/metrics/live")
        data = response.json()

        assert "timestamp" in data
        assert "concurrency" in data
        assert "performance" in data
        assert "connections" in data
        assert "system" in data

    async def test_get_live_metrics_concurrency_structure(self, api_client):
        """Test live metrics concurrency section structure."""
        response = await api_client.get("/metrics/live")
        data = response.json()

        concurrency = data["concurrency"]
        assert "pdf" in concurrency
        assert "batch" in concurrency

        pdf = concurrency["pdf"]
        assert "limit" in pdf
        assert "active" in pdf
        assert "available" in pdf
        assert "utilization_percent" in pdf

    async def test_get_live_metrics_concurrency_calculation(
        self, mock_pdf_semaphore, mock_batch_semaphore, mock_job_manager, mock_http_client
    ):
        """Test live metrics calculates concurrency correctly."""

        async def mock_get_pdf_semaphore():
            return mock_pdf_semaphore

        async def mock_get_batch_semaphore():
            return mock_batch_semaphore

        app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
        app.dependency_overrides[get_batch_semaphore] = mock_get_batch_semaphore
        app.state.job_manager = mock_job_manager
        app.state.http_client = mock_http_client

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/metrics/live")

            data = response.json()
            pdf = data["concurrency"]["pdf"]

            # PDF: 12 limit, 8 available, 4 in use
            assert pdf["limit"] == 12
            assert pdf["available"] == 8
            assert pdf["active"] == 4
            assert pdf["utilization_percent"] == pytest.approx(33.3, abs=0.1)
        finally:
            app.dependency_overrides.pop(get_pdf_semaphore, None)
            app.dependency_overrides.pop(get_batch_semaphore, None)

    async def test_get_live_metrics_connections_structure(self, api_client):
        """Test live metrics connections section structure."""
        response = await api_client.get("/metrics/live")
        data = response.json()

        connections = data["connections"]
        assert "redis" in connections
        assert "http_client" in connections

    async def test_get_live_metrics_system_info(self, api_client):
        """Test live metrics system section includes expected info."""
        response = await api_client.get("/metrics/live")
        data = response.json()

        system = data["system"]
        assert "cpu_cores" in system
        assert "pdf_scaling" in system
        assert "batch_scaling" in system


@pytest.mark.asyncio
class TestLiveMetricsErrorHandling:
    """Tests for error handling in live metrics endpoint."""

    async def test_get_live_metrics_redis_error_handling(
        self, mock_pdf_semaphore, mock_batch_semaphore, mock_http_client
    ):
        """Test live metrics handles Redis errors gracefully."""
        # Create a job manager that raises an exception
        mock_job_manager = AsyncMock()
        mock_job_manager.get_connection_stats = AsyncMock(
            side_effect=Exception("Redis connection failed")
        )

        async def mock_get_pdf_semaphore():
            return mock_pdf_semaphore

        async def mock_get_batch_semaphore():
            return mock_batch_semaphore

        app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
        app.dependency_overrides[get_batch_semaphore] = mock_get_batch_semaphore
        app.state.job_manager = mock_job_manager
        app.state.http_client = mock_http_client

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/metrics/live")

            # Should still return 200, with error in redis stats
            assert response.status_code == 200
            data = response.json()
            assert data["connections"]["redis"]["status"] == "error"
            assert "Redis connection failed" in data["connections"]["redis"]["error"]
        finally:
            app.dependency_overrides.pop(get_pdf_semaphore, None)
            app.dependency_overrides.pop(get_batch_semaphore, None)

    async def test_get_live_metrics_http_error_handling(
        self, mock_pdf_semaphore, mock_batch_semaphore, mock_job_manager
    ):
        """Test live metrics handles HTTP client errors gracefully."""
        # Create an http_client that raises an exception
        mock_http_client = MagicMock()
        mock_http_client.get_connection_stats = MagicMock(
            side_effect=Exception("HTTP client error")
        )

        async def mock_get_pdf_semaphore():
            return mock_pdf_semaphore

        async def mock_get_batch_semaphore():
            return mock_batch_semaphore

        app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
        app.dependency_overrides[get_batch_semaphore] = mock_get_batch_semaphore
        app.state.job_manager = mock_job_manager
        app.state.http_client = mock_http_client

        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/metrics/live")

            # Should still return 200, with error in http stats
            assert response.status_code == 200
            data = response.json()
            assert data["connections"]["http_client"]["status"] == "error"
            assert "HTTP client error" in data["connections"]["http_client"]["error"]
        finally:
            app.dependency_overrides.pop(get_pdf_semaphore, None)
            app.dependency_overrides.pop(get_batch_semaphore, None)

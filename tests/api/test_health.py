from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader import __version__


@pytest.mark.unit
class TestHealthEndpoint:
    """Test health endpoint service availability reporting."""

    def test_health_check_without_redis(self, api_client, env_no_redis):
        """Test health check response structure without Redis."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == __version__

        assert "services" in data
        assert "batch_processing" in data["services"]
        assert "pdf_generation" in data["services"]

        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is False
        assert batch_service["reason"] == "Redis connection (REDIS_URI) required"
        assert "max_concurrent_downloads" not in batch_service

        pdf_service = data["services"]["pdf_generation"]
        assert isinstance(pdf_service["available"], bool)

    def test_health_check_with_redis(self, api_client, env_with_redis):
        """Test health check when Redis-backed JobManager is connected."""
        mock_jm = MagicMock()
        mock_jm.redis_client = AsyncMock()
        mock_jm.redis_client.ping = AsyncMock(return_value=True)

        with patch.object(api_client.app.state, "job_manager", mock_jm):
            response = api_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == __version__

        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is True
        assert "max_concurrent_downloads" in batch_service

    def test_health_endpoint_shows_auth_disabled(self, api_client, env_no_auth):
        """Test health endpoint shows authentication is disabled."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is False
        assert data["auth_methods"] is None

    def test_health_endpoint_shows_auth_structure(self, api_client, env_with_auth):
        """Test health endpoint returns auth status structure when auth is enabled."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is True
        assert data["auth_methods"] == [
            "Authorization: Bearer <api_key>",
            "X-API-Key: <api_key>",
        ]

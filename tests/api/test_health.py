import pytest


@pytest.mark.integration
class TestHealthEndpoint:
    """Test health endpoint service availability reporting."""

    def test_health_check_without_redis(self, api_client, env_no_redis):
        """Test health check when REDIS_URI is not set."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.5"

        # Check service status
        assert "services" in data
        assert "batch_processing" in data["services"]
        assert "pdf_generation" in data["services"]

        # Check batch processing service (should be unavailable without Redis)
        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is False
        assert batch_service["reason"] == "Redis connection (REDIS_URI) required"
        assert "max_concurrent_downloads" not in batch_service

        # Check PDF generation service structure
        pdf_service = data["services"]["pdf_generation"]
        assert "available" in pdf_service
        # PDF service availability depends on whether Playwright is installed
        # which may not be the case in all test environments

    def test_health_check_with_redis(self, api_client, env_with_redis):
        """Test health check when REDIS_URI is set."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.1.5"

        # Check batch processing service structure
        batch_service = data["services"]["batch_processing"]
        assert "available" in batch_service
        # Batch service availability depends on whether Redis is actually running
        # The env_with_redis fixture sets REDIS_URI but doesn't start Redis

    def test_health_endpoint_shows_auth_disabled(self, api_client, env_no_auth):
        """Test health endpoint shows authentication is disabled."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is False
        assert data["auth_methods"] is None

    def test_health_endpoint_shows_auth_structure(self, api_client, env_no_redis):
        """Test health endpoint returns auth status structure."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "auth_enabled" in data
        # auth_methods may be None or list depending on whether auth is enabled

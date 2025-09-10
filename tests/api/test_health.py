from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


class TestHealthEndpoint:
    def test_health_check_without_redis(self, env_no_redis):
        # Test health check when REDIS_URI is not set
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.0.1"

        # Check service status
        assert "services" in data
        assert "batch_processing" in data["services"]
        assert "pdf_generation" in data["services"]

        # Check batch processing service (should be unavailable without Redis)
        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is False
        assert batch_service["reason"] == "Redis connection (REDIS_URI) required"
        assert "max_concurrent_downloads" not in batch_service

        # Check PDF generation service (should still be available)
        pdf_service = data["services"]["pdf_generation"]
        assert pdf_service["available"] is True
        assert "max_concurrent_pdfs" in pdf_service
        assert "current_active_pdfs" in pdf_service
        assert "available_slots" in pdf_service

    def test_health_check_with_redis(self, env_with_redis):
        # Test health check when REDIS_URI is set
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.0.1"

        # Check batch processing service (should be available with Redis)
        batch_service = data["services"]["batch_processing"]
        assert batch_service["available"] is True
        assert "max_concurrent_downloads" in batch_service
        assert "current_active_downloads" in batch_service
        assert "available_slots" in batch_service
        assert "reason" not in batch_service

    def test_health_endpoint_shows_auth_disabled(self, env_no_auth):
        """Test health endpoint shows authentication is disabled."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is False
        assert data["auth_methods"] is None

    def test_health_endpoint_shows_auth_enabled(self, env_with_auth):
        """Test health endpoint shows authentication is enabled."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is True
        assert isinstance(data["auth_methods"], list)

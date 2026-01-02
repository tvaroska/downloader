"""Smoke tests for server startup and health endpoint."""

import pytest


@pytest.mark.smoke
class TestHealthEndpoint:
    def test_health_check_basic(self, api_client, env_no_redis):
        """Test health endpoint returns basic health information."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["version"] == "0.2.0"
        assert "services" in data

    def test_health_endpoint_shows_auth_disabled(self, api_client, env_no_auth):
        """Test health endpoint shows authentication is disabled."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["auth_enabled"] is False
        assert data["auth_methods"] is None

    def test_health_endpoint_structure(self, api_client, env_no_redis):
        """Test health endpoint returns expected structure."""
        response = api_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
        assert "auth_enabled" in data
        assert "services" in data

"""Tests for batch processing authentication scenarios."""

from src.downloader.dependencies import get_http_client, get_job_manager_dependency
from src.downloader.main import app


class TestBatchAuthentication:
    """Test batch processing authentication scenarios."""

    def test_batch_no_auth_required(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test batch works when no authentication is required."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_auth_required_no_key(self, api_client, env_with_auth, mock_job_manager):
        """Test batch fails when auth required but no key provided."""

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 401
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == "authentication_required"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_auth_bearer_token_valid(
        self,
        api_client,
        env_with_auth,
        mock_job_manager,
        mock_http_client,
        auth_headers,
    ):
        """Test batch works with valid Bearer token."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = api_client.post(
                "/batch",
                json=batch_request,
                headers=auth_headers,
            )
            assert response.status_code == 200
            assert response.json()["job_id"] is not None
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

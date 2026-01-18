"""Tests for batch processing request validation."""

from src.downloader.dependencies import get_job_manager_dependency
from src.downloader.main import app


class TestBatchValidation:
    """Test batch request validation."""

    def test_batch_validation_errors(self, api_client, mock_job_manager):
        """Test batch request validation."""

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            # Empty URLs list
            response = api_client.post("/batch", json={"urls": []})
            assert response.status_code == 422

            # Too many URLs - this will be caught by Pydantic validation
            urls = [{"url": f"https://example{i}.com"} for i in range(51)]
            response = api_client.post("/batch", json={"urls": urls})
            assert response.status_code == 422
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_empty_request(self, api_client, mock_job_manager):
        """Test batch endpoint with invalid request body."""

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.post("/batch", json={})
            assert response.status_code == 422
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_unavailable_without_redis(self, api_client):
        """Test that batch endpoint returns 503 when job_manager is not available."""
        # Don't override the job_manager dependency - it will return None from app.state
        # which means no Redis is configured

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 503

            data = response.json()
            assert "detail" in data
            detail = data["detail"]
            assert detail["success"] is False
            assert detail["error_type"] == "service_unavailable"
            assert "Redis connection (REDIS_URI) is required" in detail["error"]
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

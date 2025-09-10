"""Tests for batch processing request validation."""

import pytest

from src.downloader.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


class TestBatchValidation:
    """Test batch request validation."""

    def test_batch_validation_errors(self, env_with_redis):
        """Test batch request validation."""
        # Empty URLs list
        response = client.post("/batch", json={"urls": []})
        assert response.status_code == 422

        # Too many URLs - this will be caught by Pydantic validation
        urls = [{"url": f"https://example{i}.com"} for i in range(51)]
        response = client.post("/batch", json={"urls": urls})
        assert response.status_code == 422

    def test_batch_empty_request(self, env_with_redis):
        """Test batch endpoint with invalid request body."""
        response = client.post("/batch", json={})
        assert response.status_code == 422

    def test_batch_unavailable_without_redis(self, env_no_redis):
        """Test that batch endpoint returns 503 when REDIS_URI is not set."""
        batch_request = {
            "urls": [{"url": "https://example.com"}],
            "default_format": "text",
        }

        response = client.post("/batch", json=batch_request)
        assert response.status_code == 503

        data = response.json()
        assert "detail" in data
        detail = data["detail"]
        assert detail["success"] is False
        assert detail["error_type"] == "service_unavailable"
        assert "Redis connection (REDIS_URI) is required" in detail["error"]
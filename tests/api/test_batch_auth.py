"""Tests for batch processing authentication scenarios."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.downloader.main import app

client = TestClient(app)


class TestBatchAuthentication:
    """Test batch processing authentication scenarios."""

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_no_auth_required(self, mock_get_client, mock_process_job, env_with_redis, mock_job_manager, mock_http_client):
        """Test batch works when no authentication is required."""
        mock_get_client.return_value = mock_http_client

        with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
            batch_request = {
                "urls": [{"url": "https://example.com"}],
                "default_format": "text",
            }

            response = client.post("/batch", json=batch_request)
            assert response.status_code == 200
            assert response.json()["job_id"] is not None

    def test_batch_auth_required_no_key(self, env_with_redis, mock_job_manager):
        """Test batch fails when auth required but no key provided."""
        with patch.dict({"DOWNLOADER_KEY": "test-key"}, clear=False):
            with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
                batch_request = {
                    "urls": [{"url": "https://example.com"}],
                    "default_format": "text",
                }

                response = client.post("/batch", json=batch_request)
                assert response.status_code == 401
                data = response.json()["detail"]
                assert data["success"] is False
                assert data["error_type"] == "authentication_required"

    @patch("src.downloader.api.process_background_batch_job", new_callable=AsyncMock)
    @patch("src.downloader.api.get_client")
    def test_batch_auth_bearer_token_valid(self, mock_get_client, mock_process_job, env_with_redis, mock_job_manager, mock_http_client, auth_headers):
        """Test batch works with valid Bearer token."""
        with patch.dict({"DOWNLOADER_KEY": "test-key"}, clear=False):
            mock_get_client.return_value = mock_http_client

            with patch("src.downloader.api.get_job_manager", return_value=mock_job_manager):
                batch_request = {
                    "urls": [{"url": "https://example.com"}],
                    "default_format": "text",
                }

                response = client.post(
                    "/batch",
                    json=batch_request,
                    headers=auth_headers,
                )
                assert response.status_code == 200
                assert response.json()["job_id"] is not None
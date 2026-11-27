"""API tests for `/batch` job submission, edge cases, and result payload caps."""

import asyncio
import json
from datetime import UTC
from unittest.mock import AsyncMock

import pytest

from downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
)
from downloader.job_manager import JobStatus
from downloader.main import app
from downloader.models.responses import BatchRequest, BatchURLRequest
from downloader.services.batch_processor import (
    process_background_batch_job,
)


class TestBatchProcessing:
    """Test core batch processing functionality."""

    def test_batch_available_with_redis(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test that batch endpoint works when job_manager is available."""

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

            data = response.json()
            assert data["job_id"] == "test_job_id"
            assert data["status"] == "pending"
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_batch_basic_success(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """Test basic batch processing with successful URLs."""

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            batch_request = {
                "urls": [
                    {"url": "https://example.com"},
                    {"url": "https://test.com", "format": "markdown"},
                ],
                "default_format": "text",
                "concurrency_limit": 2,
                "timeout_per_url": 30,
            }

            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200

            data = response.json()
            assert data["job_id"] == "test_job_id"
            mock_job_manager.create_job.assert_awaited_once()
            mock_job_manager.start_background_job.assert_awaited_once()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_submit_batch_persists_sanitized_request_without_cleartext_secret(
        self,
        api_client,
        mock_job_manager,
        mock_http_client,
    ):
        """`POST /batch` strips cleartext `webhook_secret` in all mode and seals it in api mode (`R92`, `R113`)."""
        from downloader.config import Settings
        from downloader.worker_queue import WorkerQueue
        from tests.fixtures.mock_fixtures import FakeRedis

        async def mock_get_http_client():
            return mock_http_client

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        prev_settings = getattr(app.state, "settings", None)
        prev_queue = getattr(app.state, "worker_queue", None)
        try:
            batch_request = {
                "urls": [
                    {"url": "https://example.com", "format": "text"},
                    {"url": "https://test.com", "format": "markdown"},
                ],
                "default_format": "text",
                "concurrency_limit": 2,
                "timeout_per_url": 15,
                "webhook_url": "https://example.com/hook",
                "webhook_secret": "super-secret-hmac-key",  # pragma: allowlist secret
            }
            # In default `all` mode, secret stays in-memory only and is omitted from Redis payload.
            response = api_client.post("/batch", json=batch_request)
            assert response.status_code == 200
            persisted_all = mock_job_manager.create_job.call_args.args[0]
            assert "webhook_secret" not in persisted_all
            assert "webhook_secret_enc" not in persisted_all
            assert "super-secret-hmac-key" not in json.dumps(persisted_all)

            # In `api` split mode, secret is sealed into `webhook_secret_enc` (never cleartext).
            mock_job_manager.create_job.reset_mock()
            split_cfg = Settings()
            split_cfg.worker.deployment_mode = "api"
            split_cfg.webhook.secret_key = "unit-test-batch-master-key"  # pragma: allowlist secret
            app.state.settings = split_cfg
            app.state.worker_queue = WorkerQueue(FakeRedis())
            response_api = api_client.post("/batch", json=batch_request)
            assert response_api.status_code == 200
            persisted_api = mock_job_manager.create_job.call_args.args[0]
            assert "webhook_secret" not in persisted_api
            assert persisted_api["webhook_secret_enc"].startswith("enc:v1:")
            assert "super-secret-hmac-key" not in json.dumps(persisted_api)
        finally:
            app.state.settings = prev_settings
            app.state.worker_queue = prev_queue
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_job_manager_dependency, None)


class TestBatchEndpointEdgeCases:
    """Test edge cases for batch API endpoints."""

    def test_submit_batch_no_redis(self, api_client):
        """Test batch submission when Redis is not available."""

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
            data = response.json()["detail"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_no_redis(self, api_client):
        """Test job status when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job-id/status")
            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_not_found(self, api_client, mock_job_manager):
        """Test job status when job does not exist."""
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/nonexistent-job/status")
            assert response.status_code == 404
            data = response.json()["detail"]
            assert data["error_type"] == "job_not_found"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_exception(self, api_client, mock_job_manager):
        """Test job status when an exception occurs."""
        mock_job_manager.get_job_info.side_effect = RuntimeError("Database error")

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/status")
            assert response.status_code == 500
            data = response.json()["detail"]
            assert data["error_type"] == "job_status_error"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_no_redis(self, api_client):
        """Test job results when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job-id/results")
            assert response.status_code == 503
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_pending(self, api_client, mock_job_manager):
        """Test job results when job is still pending."""
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.PENDING
        job_info.results_available = False
        job_info.progress = 0
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = None
        job_info.completed_at = None
        job_info.total_urls = 1
        job_info.processed_urls = 0
        job_info.successful_urls = 0
        job_info.failed_urls = 0
        job_info.error_message = None
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "pending" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_failed(self, api_client, mock_job_manager):
        """Test job results when job has failed."""
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.FAILED
        job_info.results_available = False
        job_info.progress = 50
        job_info.error_message = "Something went wrong"
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
        job_info.completed_at = None
        job_info.total_urls = 2
        job_info.processed_urls = 1
        job_info.successful_urls = 0
        job_info.failed_urls = 1
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "failed" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_cancelled(self, api_client, mock_job_manager):
        """Test job results when job was cancelled."""
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.CANCELLED
        job_info.results_available = False
        job_info.progress = 25
        job_info.error_message = None
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
        job_info.completed_at = None
        job_info.total_urls = 4
        job_info.processed_urls = 1
        job_info.successful_urls = 1
        job_info.failed_urls = 0
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get("/jobs/test-job/results")
            assert response.status_code == 400
            data = response.json()["detail"]
            assert data["error_type"] == "results_not_available"
            assert "cancelled" in data["error"].lower()
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_no_redis(self, api_client):
        """Test job cancellation when Redis is not available."""

        async def mock_get_job_manager():
            return None

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job-id")
            assert response.status_code == 503
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_not_found(self, api_client, mock_job_manager):
        """Test job cancellation when job does not exist."""
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/nonexistent-job")
            assert response.status_code == 404
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_already_completed(self, api_client, mock_job_manager):
        """Test job cancellation when job is already completed."""
        from datetime import datetime
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.COMPLETED
        job_info.results_available = True
        job_info.progress = 100
        job_info.error_message = None
        job_info.created_at = datetime.now(UTC)
        job_info.started_at = datetime.now(UTC)
        job_info.completed_at = datetime.now(UTC)
        job_info.total_urls = 2
        job_info.processed_urls = 2
        job_info.successful_urls = 2
        job_info.failed_urls = 0
        job_info.expires_at = None

        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.cancel_job.return_value = False

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job")
            assert response.status_code == 200
            data = response.json()
            assert data["success"] is False
            assert "could not be cancelled" in data["message"]
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_exception(self, api_client, mock_job_manager):
        """Test job cancellation when an exception occurs."""
        from unittest.mock import MagicMock

        job_info = MagicMock()
        job_info.job_id = "test-job"
        job_info.status = JobStatus.RUNNING

        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.cancel_job.side_effect = RuntimeError("Database error")

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete("/jobs/test-job")
            assert response.status_code == 500
            data = response.json()["detail"]
            assert data["error_type"] == "job_cancellation_error"
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)


@pytest.mark.asyncio
async def test_batch_result_aggregate_size_is_capped():
    """Aggregate batch results are bounded so a single Redis SETEX cannot grow unboundedly (R43)."""
    from datetime import datetime

    from downloader.job_manager import JobInfo, JobManager

    mock_client = AsyncMock()
    mock_client.download.return_value = (
        b"<html><body>" + b"A" * 500 + b"</body></html>",
        {
            "url": "https://example.com",
            "status_code": 200,
            "content_type": "text/html",
            "size": 526,
            "headers": {"content-type": "text/html"},
        },
    )
    mock_jm = AsyncMock()
    mock_jm.get_job_info = AsyncMock(return_value=None)
    mock_jm.update_job_status = AsyncMock()

    batch_request = BatchRequest(
        urls=[
            BatchURLRequest(url="https://example1.com"),
            BatchURLRequest(url="https://example2.com"),
            BatchURLRequest(url="https://example3.com"),
        ],
        default_format="text",
        concurrency_limit=1,
        timeout_per_url=10,
    )

    results, summary = await process_background_batch_job(
        job_id="cap-job-1",
        batch_request=batch_request,
        job_manager=mock_jm,
        batch_semaphore=asyncio.Semaphore(1),
        http_client=mock_client,
        pdf_semaphore=asyncio.Semaphore(1),
        max_result_bytes=1200,
    )

    assert len(results) == 3
    assert any(r["error_type"] == "content_too_large" and r["status_code"] == 413 for r in results)
    assert summary["failed_requests"] >= 1

    # Also verify JobManager.store_job_results enforces the cap before SETEX
    kv: dict[str, str] = {}
    redis_mock = AsyncMock()
    redis_mock.get = AsyncMock(
        return_value=JobInfo(
            job_id="cap-job-1",
            status=JobStatus.RUNNING,
            created_at=datetime.now(UTC),
            request_data={},
        ).model_dump_json()
    )
    redis_mock.setex = AsyncMock(side_effect=lambda k, _ttl, v: kv.__setitem__(k, v))
    jm = JobManager("redis://localhost:6379")
    jm.redis_client = redis_mock

    big_results = [
        {"url": f"https://example{i}.com", "success": True, "content": "X" * 400, "size": 400}
        for i in range(3)
    ]
    await jm.store_job_results(
        "cap-job-1",
        big_results,
        {"total_requests": 3},
        status=JobStatus.COMPLETED,
        total_duration=0.5,
        max_result_bytes=1100,
    )
    stored_payload = kv["job_result:cap-job-1"]
    assert len(stored_payload.encode("utf-8")) <= 1100
    parsed = json.loads(stored_payload)
    assert parsed["summary"]["truncated"] is True

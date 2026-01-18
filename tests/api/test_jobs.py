"""Tests for the job-related API endpoints."""

from datetime import datetime, timezone

from src.downloader.dependencies import get_job_manager_dependency
from src.downloader.job_manager import JobInfo, JobResult, JobStatus
from src.downloader.main import app


class TestJobEndpoints:
    def test_get_job_status_found(self, api_client, mock_job_manager):
        """Test getting the status of an existing job."""
        job_id = "test-job-id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.RUNNING,
            progress=50,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get(f"/jobs/{job_id}/status")
            assert response.status_code == 200
            data = response.json()
            assert data["job_id"] == job_id
            assert data["status"] == "running"
            assert data["progress"] == 50
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_status_not_found(self, api_client, mock_job_manager):
        """Test getting the status of a non-existent job."""
        job_id = "not-found-id"
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get(f"/jobs/{job_id}/status")
            assert response.status_code == 404
            assert "Job not-found-id not found" in response.text
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_found(self, api_client, mock_job_manager):
        """Test getting the results of a completed job."""
        job_id = "test-job-id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            results_available=True,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        job_result = JobResult(
            job_id=job_id,
            status=JobStatus.COMPLETED,
            total_duration=10.5,
            results=[{"success": True}],
            summary={},
            created_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
        )
        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.get_job_results.return_value = job_result

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get(f"/jobs/{job_id}/results")
            assert response.status_code == 200
            data = response.json()
            assert data["job_id"] == job_id
            assert len(data["results"]) == 1
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_get_job_results_not_available(self, api_client, mock_job_manager):
        """Test getting results for a job that is not finished."""
        job_id = "test-job-id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.RUNNING,
            results_available=False,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_job_manager.get_job_info.return_value = job_info

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.get(f"/jobs/{job_id}/results")
            assert response.status_code == 400
            assert "is still running" in response.text
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_success(self, api_client, mock_job_manager):
        """Test cancelling a job successfully."""
        job_id = "test-job-id"
        job_info = JobInfo(
            job_id=job_id,
            status=JobStatus.RUNNING,
            created_at=datetime.now(timezone.utc),
            request_data={},
        )
        mock_job_manager.get_job_info.return_value = job_info
        mock_job_manager.cancel_job.return_value = True

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete(f"/jobs/{job_id}")
            assert response.status_code == 200
            assert response.json()["success"] is True
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_cancel_job_not_found(self, api_client, mock_job_manager):
        """Test cancelling a non-existent job."""
        job_id = "not-found-id"
        mock_job_manager.get_job_info.return_value = None

        async def mock_get_job_manager():
            return mock_job_manager

        app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
        try:
            response = api_client.delete(f"/jobs/{job_id}")
            assert response.status_code == 404
        finally:
            app.dependency_overrides.pop(get_job_manager_dependency, None)

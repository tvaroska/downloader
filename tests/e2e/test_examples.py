"""E2E tests that validate example scripts run successfully."""

import os
import subprocess
import sys

import pytest


@pytest.mark.e2e
class TestExamples:
    """Test that all example scripts execute successfully."""

    @pytest.fixture(autouse=True)
    def setup_environment(self, app_base_url):
        """Set up environment variables for examples."""
        os.environ["DOWNLOADER_BASE_URL"] = app_base_url
        os.environ["DOWNLOADER_KEY"] = "test-api-key-e2e"

    def test_basic_usage_example(self):
        """Test that basic_usage.py runs successfully."""
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "examples", "basic_usage.py"
        )

        result = subprocess.run(
            [sys.executable, example_path],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Example failed: {result.stderr}"
        assert "Success" in result.stdout or "Downloaded" in result.stdout

    def test_content_formats_example(self):
        """Test that content_formats.py runs successfully."""
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "examples", "content_formats.py"
        )

        result = subprocess.run(
            [sys.executable, example_path],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 0, f"Example failed: {result.stderr}"

    def test_batch_processing_example(self):
        """Test that batch_processing.py runs successfully."""
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "examples", "batch_processing.py"
        )

        result = subprocess.run(
            [sys.executable, example_path],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, f"Example failed: {result.stderr}"
        assert "Batch job created" in result.stdout or "completed" in result.stdout.lower()

    def test_batch_job_example(self):
        """Test that batch_job_example.py runs successfully."""
        example_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "examples", "batch_job_example.py"
        )

        result = subprocess.run(
            [sys.executable, example_path],
            capture_output=True,
            text=True,
            timeout=60,
        )

        assert result.returncode == 0, f"Example failed: {result.stderr}"

    @pytest.mark.slow
    def test_concurrent_pdf_requests_example(self):
        """Test that concurrent_pdf_requests.py runs successfully."""
        example_path = os.path.join(
            os.path.dirname(__file__),
            "..", "..",
            "examples",
            "concurrent_pdf_requests.py"
        )

        result = subprocess.run(
            [sys.executable, example_path],
            capture_output=True,
            text=True,
            timeout=120,
        )

        assert result.returncode == 0, f"Example failed: {result.stderr}"
        assert "PDF" in result.stdout or "completed" in result.stdout.lower()


@pytest.mark.e2e
class TestEndpoints:
    """Test core API endpoints in E2E environment."""

    def test_health_endpoint(self, app_base_url):
        """Test that health endpoint is accessible."""
        import requests

        response = requests.get(f"{app_base_url}/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"

    def test_metrics_endpoint(self, app_base_url):
        """Test that metrics endpoint is accessible."""
        import requests

        response = requests.get(f"{app_base_url}/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "uptime" in data

    def test_download_endpoint(self, app_base_url):
        """Test basic download functionality."""
        import requests

        headers = {
            "Authorization": "Bearer test-api-key-e2e",
            "Accept": "text/plain"
        }

        response = requests.get(
            f"{app_base_url}/https://example.com",
            headers=headers,
            timeout=30
        )

        assert response.status_code == 200
        assert len(response.text) > 0

    def test_batch_endpoint_with_redis(self, app_base_url):
        """Test batch endpoint with Redis backend."""
        import requests

        headers = {
            "Authorization": "Bearer test-api-key-e2e",
            "Content-Type": "application/json"
        }

        batch_request = {
            "urls": [
                {"url": "https://example.com"},
                {"url": "https://example.org"}
            ],
            "default_format": "text"
        }

        response = requests.post(
            f"{app_base_url}/batch",
            headers=headers,
            json=batch_request,
            timeout=30
        )

        assert response.status_code == 200
        data = response.json()
        assert "job_id" in data
        assert data["status"] in ["pending", "processing", "completed"]

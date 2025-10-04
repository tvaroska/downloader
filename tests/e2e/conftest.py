"""E2E test fixtures and configuration."""

import os
import subprocess
import time

import pytest


@pytest.fixture(scope="session")
def docker_services():
    """Start docker-compose services for E2E tests."""
    compose_file = os.path.join(os.path.dirname(__file__), "docker-compose.yml")

    # Start services
    subprocess.run(
        ["docker-compose", "-f", compose_file, "up", "-d"],
        check=True,
        cwd=os.path.dirname(__file__),
    )

    # Wait for services to be healthy (check healthcheck status)
    max_attempts = 60  # 60 attempts * 2s = 2 minutes max wait
    attempt = 0
    while attempt < max_attempts:
        result = subprocess.run(
            ["docker-compose", "-f", compose_file, "ps", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=os.path.dirname(__file__),
        )
        if result.returncode == 0 and "healthy" in result.stdout:
            break
        attempt += 1
        time.sleep(2)

    if attempt >= max_attempts:
        # Log container status before failing
        subprocess.run(
            ["docker-compose", "-f", compose_file, "logs"],
            cwd=os.path.dirname(__file__),
        )
        raise RuntimeError("Services failed to become healthy within timeout")

    # Extra grace period after health check passes
    time.sleep(3)

    yield

    # Cleanup
    subprocess.run(
        ["docker-compose", "-f", compose_file, "down", "-v"],
        check=False,
        cwd=os.path.dirname(__file__),
    )


@pytest.fixture(scope="session")
def app_base_url(docker_services):
    """Base URL for the running application."""
    return os.getenv("E2E_BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def redis_url(docker_services):
    """Redis URL for E2E tests."""
    return os.getenv("E2E_REDIS_URL", "redis://localhost:6379")

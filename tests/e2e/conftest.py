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

    # Wait for services to be ready
    time.sleep(5)

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

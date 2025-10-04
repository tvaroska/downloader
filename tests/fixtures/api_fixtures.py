"""API client and authentication fixtures."""

import pytest
from fastapi.testclient import TestClient

from src.downloader.main import app


@pytest.fixture
def api_client(request):
    """
    Fixture to provide FastAPI test client with lifespan events.

    This creates a TestClient that properly handles app startup/shutdown,
    making it suitable for integration tests that need full app context.

    NOTE: Settings are reloaded by env fixtures (env_with_auth, etc) before
    this fixture is used, so we don't need to reload here.
    """
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture
def auth_headers():
    """Fixture to provide valid Bearer token authentication headers."""
    return {"Authorization": "Bearer test-key"}


@pytest.fixture
def api_key_headers():
    """Fixture to provide valid X-API-Key authentication headers."""
    return {"X-API-Key": "test-key"}

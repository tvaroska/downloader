"""Environment and configuration fixtures for tests."""

import os
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=False)
def env_no_auth():
    """Fixture to clear authentication environment variables."""
    with patch.dict(os.environ, {}, clear=True):
        from src.downloader.config import reload_settings

        reload_settings()
        yield
        reload_settings()


@pytest.fixture(autouse=False)
def env_with_auth():
    """Fixture to set authentication environment variables.

    Also clears REDIS_URI to prevent lifespan from trying to connect to Redis.
    """
    # Use patch.dict as context manager so it's active during test
    # Clear REDIS_URI to prevent Redis connection attempts during lifespan
    env_vars = {"DOWNLOADER_KEY": "test-key"}
    with patch.dict(os.environ, env_vars, clear=True):
        # Need to reload settings here while env is patched
        from src.downloader.config import reload_settings

        reload_settings()
        yield
        # Cleanup
        reload_settings()


@pytest.fixture(autouse=False)
def env_with_redis():
    """Fixture to set Redis environment variables."""
    with patch.dict(os.environ, {"REDIS_URI": "redis://localhost:6379"}, clear=True):
        from src.downloader.config import reload_settings

        reload_settings()
        yield
        reload_settings()


@pytest.fixture(autouse=False)
def env_no_redis():
    """Fixture to clear Redis environment variables."""
    with patch.dict(os.environ, {}, clear=False):
        if "REDIS_URI" in os.environ:
            del os.environ["REDIS_URI"]
        from src.downloader.config import reload_settings

        reload_settings()
        yield
        reload_settings()

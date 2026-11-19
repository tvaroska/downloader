"""Environment and configuration fixtures for tests."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest


@contextmanager
def _patched_env_and_app_settings(
    env_vars: dict[str, str],
    *,
    clear: bool = True,
    unset: tuple[str, ...] = (),
) -> Iterator[None]:
    """Patch `os.environ`, reload global settings, and keep `app.state.settings` in sync."""
    from downloader.config import reload_settings
    from downloader.main import app

    had_attr = hasattr(app.state, "settings")
    prev_app_settings = getattr(app.state, "settings", None)
    try:
        with patch.dict(os.environ, env_vars, clear=clear):
            for key in unset:
                os.environ.pop(key, None)
            refreshed = reload_settings()
            app.state.settings = refreshed
            yield
    finally:
        restored = reload_settings()
        if had_attr:
            app.state.settings = prev_app_settings if prev_app_settings is not None else restored
        elif hasattr(app.state, "settings"):
            delattr(app.state, "settings")


@pytest.fixture(autouse=False)
def env_no_auth() -> Iterator[None]:
    """Fixture to clear authentication environment variables."""
    with _patched_env_and_app_settings({}, clear=True):
        yield


@pytest.fixture(autouse=False)
def env_with_auth() -> Iterator[None]:
    """Fixture to set authentication environment variables.

    Also clears REDIS_URI to prevent lifespan from trying to connect to Redis.
    """
    with _patched_env_and_app_settings({"DOWNLOADER_KEY": "test-key"}, clear=True):
        yield


@pytest.fixture(autouse=False)
def env_with_redis() -> Iterator[None]:
    """Fixture to set Redis environment variables."""
    with _patched_env_and_app_settings({"REDIS_URI": "redis://localhost:6379"}, clear=True):
        yield


@pytest.fixture(autouse=False)
def env_no_redis() -> Iterator[None]:
    """Fixture to clear Redis environment variables."""
    with _patched_env_and_app_settings({}, clear=False, unset=("REDIS_URI",)):
        yield

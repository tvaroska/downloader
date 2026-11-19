"""Smoke tests for rate limiting configuration and active middleware enforcement (`R111`)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from downloader.config import RateLimitConfig, Settings, get_settings
from downloader.ratelimit_middleware import RateLimitMiddleware


@pytest.mark.smoke
def test_rate_limiting_config_defaults_are_valid() -> None:
    """Rate limiting defaults are enabled and parse cleanly as `count/period`."""
    settings = get_settings()
    assert settings.ratelimit.enabled is True
    for limit in (
        settings.ratelimit.default_limit,
        settings.ratelimit.download_limit,
        settings.ratelimit.batch_limit,
        settings.ratelimit.status_limit,
    ):
        count_str, period = limit.split("/")
        assert int(count_str) > 0
        assert period in {"second", "minute", "hour", "day"}


@pytest.mark.smoke
def test_rate_limiting_path_classification_maps_buckets() -> None:
    """Every route family maps to its exact bucket and configured limit."""
    cfg = Settings(
        ratelimit=RateLimitConfig(
            enabled=True,
            default_limit="11/minute",
            download_limit="22/minute",
            batch_limit="33/minute",
            status_limit="44/minute",
        )
    )
    app = FastAPI()
    app.state.settings = cfg
    mw = RateLimitMiddleware(app)

    assert mw._classify_path("/health") == ("status", "44/minute")
    assert mw._classify_path("/metrics") == ("status", "44/minute")
    assert mw._classify_path("/jobs/abc/status") == ("status", "44/minute")
    assert mw._classify_path("/batch") == ("batch", "33/minute")
    assert mw._classify_path("/schedules") == ("default", "11/minute")
    assert mw._classify_path("/https://example.com") == ("download", "22/minute")


@pytest.mark.smoke
def test_rate_limiting_middleware_enforces_429_on_limit_exceeded() -> None:
    """RateLimitMiddleware enforces a real 429 once the window limit is exhausted (`R111`)."""
    app = FastAPI()
    app.state.settings = Settings(
        ratelimit=RateLimitConfig(
            enabled=True,
            storage_uri="memory://",
            status_limit="1/minute",
            headers_enabled=True,
        )
    )
    app.add_middleware(RateLimitMiddleware)

    @app.get("/health")
    async def _health() -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        first = client.get("/health")
        second = client.get("/health")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "1"
    assert first.headers["X-RateLimit-Remaining"] == "0"
    assert second.status_code == 429
    assert second.json()["detail"]["error_type"] == "rate_limit_exceeded"
    assert int(second.headers["Retry-After"]) >= 1

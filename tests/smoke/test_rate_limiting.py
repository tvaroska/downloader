"""Tests for rate limiting functionality."""

import pytest


@pytest.mark.smoke
def test_rate_limiting_config():
    """Test rate limiting configuration."""
    from src.downloader.config import get_settings

    settings = get_settings()

    # Verify rate limiting configuration exists
    assert hasattr(settings, 'ratelimit')
    assert hasattr(settings.ratelimit, 'enabled')
    assert hasattr(settings.ratelimit, 'default_limit')
    assert hasattr(settings.ratelimit, 'download_limit')
    assert hasattr(settings.ratelimit, 'batch_limit')
    assert hasattr(settings.ratelimit, 'status_limit')

    # Verify default values
    assert isinstance(settings.ratelimit.enabled, bool)
    assert isinstance(settings.ratelimit.default_limit, str)
    assert "/" in settings.ratelimit.default_limit  # Format: "count/period"


@pytest.mark.smoke
def test_rate_limiting_middleware_initialization():
    """Test that rate limiting middleware can be initialized."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from src.downloader.ratelimit_middleware import RateLimitMiddleware
    from src.downloader.config import get_settings
    from fastapi import FastAPI

    settings = get_settings()

    # Create a test app and limiter
    app = FastAPI()
    limiter = Limiter(
        key_func=get_remote_address,
        storage_uri="memory://",
    )

    # Should initialize without errors
    middleware = RateLimitMiddleware(app, limiter)

    # Verify middleware has required attributes
    assert hasattr(middleware, 'limiter')
    assert hasattr(middleware, 'settings')
    assert hasattr(middleware, 'rate_limits')


@pytest.mark.smoke
def test_rate_limiting_patterns():
    """Test rate limiting pattern matching."""
    from slowapi import Limiter
    from slowapi.util import get_remote_address
    from src.downloader.ratelimit_middleware import RateLimitMiddleware
    from fastapi import FastAPI

    app = FastAPI()
    limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
    middleware = RateLimitMiddleware(app, limiter)

    # Test pattern matching for different paths
    health_limit = middleware._get_rate_limit_for_path("/health")
    metrics_limit = middleware._get_rate_limit_for_path("/metrics")
    batch_limit = middleware._get_rate_limit_for_path("/batch/jobs")
    download_limit = middleware._get_rate_limit_for_path("/https://example.com")

    # Verify appropriate limits are applied
    # If rate limiting is enabled, limits should be strings
    if middleware.settings.ratelimit.enabled:
        assert health_limit is not None
        assert metrics_limit is not None
        assert batch_limit is not None
        assert download_limit is not None

        # Status endpoints should have higher limits
        assert "/" in health_limit
        assert "/" in metrics_limit
    else:
        # If disabled, all should be None
        assert health_limit is None


@pytest.mark.smoke
def test_main_app_has_rate_limiter():
    """Test that main app has rate limiter configured."""
    from src.downloader.main import app

    # App should have limiter in state
    assert hasattr(app.state, 'limiter')
    assert app.state.limiter is not None


@pytest.mark.smoke
def test_rate_limiting_enabled_in_config():
    """Test that rate limiting is enabled by default."""
    from src.downloader.config import get_settings

    settings = get_settings()

    # Rate limiting should be enabled by default
    assert settings.ratelimit.enabled is True


@pytest.mark.smoke
def test_rate_limiting_limits_are_valid():
    """Test that all configured rate limits have valid format."""
    from src.downloader.config import get_settings

    settings = get_settings()

    # All limits should be in format "count/period"
    limits = [
        settings.ratelimit.default_limit,
        settings.ratelimit.download_limit,
        settings.ratelimit.batch_limit,
        settings.ratelimit.status_limit,
    ]

    for limit in limits:
        assert isinstance(limit, str)
        assert "/" in limit
        parts = limit.split("/")
        assert len(parts) == 2
        # First part should be a number
        assert parts[0].isdigit()
        # Second part should be a time period
        assert parts[1] in ["second", "minute", "hour", "day"]

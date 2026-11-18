"""API error envelope tests (`R40`)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from downloader.dependencies import get_http_client
from downloader.errors import classify_domain_error
from downloader.http_client import HTTPClientError
from downloader.main import app


@pytest.mark.unit
def test_validation_errors_use_error_envelope(api_client, env_no_auth) -> None:
    """URL/query validation errors return the documented `{detail: {success, error, error_type}}` envelope."""
    response = api_client.get("/https://example.com/page?unknown_param=1")
    assert response.status_code == 400
    body = response.json()
    assert "detail" in body
    detail = body["detail"]
    assert detail["success"] is False
    assert detail["error_type"] == "validation_error"
    assert isinstance(detail["error"], str) and detail["error"]


@pytest.mark.unit
def test_render_timeout_uses_worker_timeout_error_type() -> None:
    """Remote render TimeoutError in render context maps to 504 worker_timeout."""
    classified = classify_domain_error(TimeoutError("worker timed out"), context="render")
    assert classified.status_code == 504
    assert classified.error_type == "worker_timeout"


@pytest.mark.unit
def test_upstream_429_preserves_http_error_type(api_client, env_no_auth) -> None:
    """Upstream 429 from origin is tagged error_type='http_error' so SDK can distinguish it."""
    mock_client = AsyncMock()
    mock_client.download.side_effect = HTTPClientError(
        "HTTP 429: Too Many Requests", status_code=429
    )
    app.dependency_overrides[get_http_client] = lambda: mock_client
    try:
        response = api_client.get("/https://example.com")
        assert response.status_code == 429
        detail = response.json()["detail"]
        assert detail["error_type"] == "http_error"
    finally:
        app.dependency_overrides.pop(get_http_client, None)

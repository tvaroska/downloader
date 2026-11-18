"""Unit & API tests for strict request models and timeout_ms wiring (`R41`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from downloader.models.responses import BatchRequest, BatchURLRequest, RenderRequest


@pytest.mark.unit
def test_render_request_rejects_unknown_fields() -> None:
    """RenderRequest, BatchRequest, and BatchURLRequest forbid unknown extra fields."""
    with pytest.raises(ValidationError):
        RenderRequest(url="https://example.com", unknown_field="nope")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        BatchURLRequest(url="https://example.com", extra_key=1)  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            unexpected=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_batch_request_validates_format_and_url_length() -> None:
    """Batch format values are typed as Literals and batch URLs are capped at 2048 chars."""
    with pytest.raises(ValidationError):
        BatchURLRequest(url="https://example.com", format="docx")  # type: ignore[arg-type]

    with pytest.raises(ValidationError):
        BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="xml",  # type: ignore[arg-type]
        )

    with pytest.raises(ValidationError):
        BatchURLRequest(url="https://example.com/" + "a" * 2048)


@pytest.mark.unit
def test_render_request_timeout_ms_bounds_and_route_wiring(api_client, env_no_auth) -> None:
    """timeout_ms is validated (1000..120000) and enforced as a deadline on POST /render."""
    req = RenderRequest(url="https://example.com", timeout_ms=5000)
    assert req.timeout_ms == 5000

    with pytest.raises(ValidationError):
        RenderRequest(url="https://example.com", timeout_ms=500)

    with pytest.raises(ValidationError):
        RenderRequest(url="https://example.com", timeout_ms=200_000)

    async def slow_convert(*_args, **_kwargs):
        await asyncio.sleep(5.0)

    with (
        patch(
            "downloader.routes.render.validate_url_async",
            new=AsyncMock(return_value="https://example.com"),
        ),
        patch("downloader.routes.render.convert", side_effect=slow_convert),
        patch("downloader.routes.render.asyncio.timeout", side_effect=TimeoutError("deadline")),
    ):
        resp = api_client.post(
            "/render",
            json={"url": "https://example.com", "timeout_ms": 1000},
        )
        assert resp.status_code == 504
        assert resp.json()["detail"]["error_type"] == "worker_timeout"

"""Structured browser rendering and interaction scripting endpoint (`POST /render`)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..auth import get_api_key
from ..config import Settings
from ..dependencies import (
    BrowserRendererDep,
    DomainThrottleDep,
    RobotsCheckerDep,
    SettingsDep,
)
from ..errors import classify_domain_error, log_classified_error
from ..models.responses import ErrorResponse, HTTPErrorResponse, RenderRequest
from ..ports import HTTPTimeoutError, WorkerTimeoutError
from ..services.content_negotiation import sanitize_original_url_header
from ..services.content_processor import ConversionDeps, ConversionRequest, convert
from ..validation import canonical_host, redact_url_for_log, validate_url_async

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/render",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid URL or browser action"},
        403: {"model": HTTPErrorResponse, "description": "Disallowed by origin robots.txt"},
        408: {"model": HTTPErrorResponse, "description": "Selector or render timeout"},
        413: {"model": HTTPErrorResponse, "description": "Rendered output exceeds size limit"},
        429: {"model": HTTPErrorResponse, "description": "Crawl-delay exceeded"},
        500: {"model": HTTPErrorResponse, "description": "Render failure"},
        503: {"model": HTTPErrorResponse, "description": "Browser rendering at capacity"},
        504: {"model": HTTPErrorResponse, "description": "Remote worker render timeout"},
    },
)
async def render_url(  # noqa: PLR0913
    render_request: RenderRequest,
    request: Request,
    renderer: BrowserRendererDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    settings: SettingsDep,
    _api_key: str | None = Depends(get_api_key),
) -> Response:
    """Render a URL via `BrowserRenderer` with optional declarative actions and capture output."""
    active_settings: Settings = getattr(request.app.state, "settings", None) or settings
    try:
        validated_url = await validate_url_async(render_request.url, active_settings)
        logger.info(
            f"Processing POST /render for {redact_url_for_log(validated_url)} "
            f"(output={render_request.output}, actions={len(render_request.actions)})"
        )

        if active_settings.crawling.respect_robots_txt:
            crawl_delay = await robots_checker.check_allowed(
                validated_url,
                active_settings.crawling.user_agent,
            )
            await domain_throttle.wait_if_needed(canonical_host(validated_url), crawl_delay)

        conv_req = ConversionRequest(
            url=validated_url,
            output_format=render_request.output,
            force_render=True,
            wait_for_selector=render_request.wait_for,
            actions=render_request.actions or None,
            full_page=render_request.full_page,
            screenshot_format=render_request.screenshot_format,
            timeout_ms=render_request.timeout_ms,
        )
        if render_request.timeout_ms is not None:
            try:
                async with asyncio.timeout(render_request.timeout_ms / 1000.0):
                    result = await convert(conv_req, ConversionDeps(renderer=renderer))
            except TimeoutError as exc:
                if isinstance(exc, WorkerTimeoutError):
                    raise
                raise HTTPTimeoutError(
                    f"Render request timed out after {render_request.timeout_ms}ms"
                ) from exc
        else:
            result = await convert(conv_req, ConversionDeps(renderer=renderer))
        headers = dict(result.headers)
        if "X-Original-URL" in headers:
            headers["X-Original-URL"] = sanitize_original_url_header(headers["X-Original-URL"])
        return Response(
            content=result.body,
            media_type=result.media_type,
            headers=headers,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Map every domain and unexpected runtime error through the single classification table.
        classified = classify_domain_error(exc, context="render")
        log_classified_error(logger, render_request.url, exc, classified)
        raise HTTPException(
            status_code=classified.status_code,
            detail=ErrorResponse(
                error=classified.message,
                error_type=classified.error_type,
            ).model_dump(),
            headers=classified.headers,
        ) from exc

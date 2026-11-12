"""Structured browser rendering and interaction scripting endpoint (`POST /render`)."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ..auth import get_api_key
from ..browser.actions import BrowserActionError
from ..content_converter import SelectorTimeoutError
from ..crawling import RobotsDisallowedError
from ..dependencies import (
    DomainThrottleDep,
    PDFSemaphoreDep,
    RobotsCheckerDep,
    SettingsDep,
    WorkerQueueDep,
)
from ..models.responses import ErrorResponse, HTTPErrorResponse, RenderRequest
from ..pdf_generator import PDFGeneratorError
from ..services.content_processor import (
    handle_html_response,
    handle_markdown_response,
    handle_pdf_response,
    handle_screenshot_response,
    handle_text_response,
)
from ..validation import URLValidationError, validate_url_async

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/render",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid URL or browser action"},
        403: {"model": HTTPErrorResponse, "description": "Disallowed by origin robots.txt"},
        408: {"model": HTTPErrorResponse, "description": "Selector or render timeout"},
        500: {"model": HTTPErrorResponse, "description": "Render failure"},
        503: {"model": HTTPErrorResponse, "description": "Browser rendering at capacity"},
        504: {"model": HTTPErrorResponse, "description": "Remote worker render timeout"},
    },
)
async def render_url(
    render_request: RenderRequest,
    request: Request,
    pdf_semaphore: PDFSemaphoreDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    settings: SettingsDep,
    worker_queue: WorkerQueueDep = None,
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """Render a URL in Playwright with optional declarative actions and capture output."""
    active_settings = getattr(request.app.state, "settings", None) or settings
    try:
        validated_url = await validate_url_async(render_request.url, active_settings)
        logger.info(
            f"Processing POST /render for {validated_url} "
            f"(output={render_request.output}, actions={len(render_request.actions)})"
        )

        if active_settings.crawling.respect_robots_txt:
            crawl_delay = await robots_checker.check_allowed(
                validated_url,
                active_settings.crawling.user_agent,
            )
            await domain_throttle.wait_if_needed(urlparse(validated_url).netloc, crawl_delay)

        remote_queue = (
            worker_queue
            if (
                active_settings.worker.deployment_mode == "api"
                and active_settings.worker.remote_browser_enabled
            )
            else None
        )
        remote_timeout = active_settings.worker.remote_browser_timeout

        if render_request.output == "pdf":
            return await handle_pdf_response(
                validated_url,
                None,
                pdf_semaphore,
                actions=render_request.actions or None,
                wait_for_selector=render_request.wait_for,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if render_request.output == "screenshot":
            return await handle_screenshot_response(
                validated_url,
                None,
                pdf_semaphore,
                screenshot_format=render_request.screenshot_format,
                full_page=render_request.full_page,
                wait_for_selector=render_request.wait_for,
                actions=render_request.actions or None,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if render_request.output == "text":
            return await handle_text_response(
                validated_url,
                None,
                None,
                True,
                render_request.wait_for,
                pdf_semaphore=pdf_semaphore,
                actions=render_request.actions or None,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if render_request.output == "markdown":
            return await handle_markdown_response(
                validated_url,
                None,
                None,
                True,
                render_request.wait_for,
                pdf_semaphore=pdf_semaphore,
                actions=render_request.actions or None,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        return await handle_html_response(
            validated_url,
            None,
            None,
            True,
            render_request.wait_for,
            pdf_semaphore=pdf_semaphore,
            actions=render_request.actions or None,
            worker_queue=remote_queue,
            remote_timeout=remote_timeout,
        )

    except RobotsDisallowedError as e:
        raise HTTPException(
            status_code=403,
            detail=ErrorResponse(error=str(e), error_type="robots_disallowed").model_dump(),
        ) from e
    except URLValidationError as e:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(e), error_type="validation_error").model_dump(),
        ) from e
    except BrowserActionError as e:
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(e), error_type="browser_action_error").model_dump(),
        ) from e
    except SelectorTimeoutError as e:
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="selector_timeout_error").model_dump(),
        ) from e
    except TimeoutError as e:
        raise HTTPException(
            status_code=504,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        ) from e
    except PDFGeneratorError as e:
        if e.status_code is not None and 400 <= e.status_code < 500:
            raise HTTPException(
                status_code=e.status_code,
                detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
            ) from e
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=f"Browser render failed: {e}",
                error_type="render_error",
            ).model_dump(),
        ) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Unexpected error rendering {render_request.url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Internal server error",
                error_type="internal_error",
            ).model_dump(),
        ) from e

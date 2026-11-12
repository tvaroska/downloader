"""Single URL download endpoint."""

import logging
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response

from ..auth import get_api_key
from ..browser.actions import BrowserActionError, parse_actions_param
from ..content_converter import SelectorTimeoutError
from ..crawling import RobotsDisallowedError
from ..dependencies import (
    DomainThrottleDep,
    HTTPClientDep,
    PDFSemaphoreDep,
    RobotsCheckerDep,
    SettingsDep,
    WorkerQueueDep,
)
from ..http_client import (
    ContentTooLargeError,
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
)
from ..models.responses import ErrorResponse, HTTPErrorResponse, ResponseMetadata
from ..pdf_generator import PDFGeneratorError
from ..services.content_processor import (
    handle_html_response,
    handle_json_response,
    handle_markdown_response,
    handle_multi_format_response,
    handle_pdf_response,
    handle_raw_response,
    handle_screenshot_response,
    handle_text_response,
    parse_accept_header,
    parse_accept_headers,
)
from ..transformers.ocr import OCRError, OCRUnavailableError, validate_ocr_language
from ..validation import URLValidationError, validate_url_async

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{url:path}",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid URL or request"},
        403: {"model": HTTPErrorResponse, "description": "Disallowed by origin robots.txt"},
        408: {"model": HTTPErrorResponse, "description": "Request timeout"},
        413: {"model": HTTPErrorResponse, "description": "Response content exceeds size limit"},
        422: {"model": HTTPErrorResponse, "description": "OCR image processing error"},
        429: {"model": HTTPErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": HTTPErrorResponse, "description": "Internal server error"},
        502: {"model": HTTPErrorResponse, "description": "Upstream server error"},
        503: {"model": HTTPErrorResponse, "description": "Service temporarily unavailable"},
        504: {"model": HTTPErrorResponse, "description": "Remote worker render timeout"},
    },
)
async def download_url(
    request: Request,
    http_client: HTTPClientDep,
    pdf_semaphore: PDFSemaphoreDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    settings: SettingsDep,
    worker_queue: WorkerQueueDep = None,
    url: str = Path(..., description="The URL to download"),
    accept: str | None = Header(None, description="Accept header for content negotiation"),
    render: bool = Query(False, description="Force Playwright rendering, bypass auto-detection"),
    wait_for: str | None = Query(
        None,
        description="CSS selector to wait for before returning. Implies render=true.",
        max_length=500,
    ),
    output: Literal["screenshot", "pdf"] | None = Query(
        None,
        description="Explicit binary output format toggle (screenshot or pdf)",
    ),
    full_page: bool = Query(
        False,
        description="Capture full scrollable page when rendering a screenshot",
    ),
    screenshot_format: Literal["png", "jpeg"] = Query(
        "png",
        description="Image encoding when rendering a screenshot (png or jpeg)",
    ),
    actions: str | None = Query(
        None,
        max_length=8192,
        description="JSON array of declarative Playwright actions to execute prior to capture",
    ),
    ocr: bool = Query(
        False,
        description="Require OCR extraction (validates that origin returns an image)",
    ),
    ocr_lang: str | None = Query(
        None,
        max_length=64,
        description="Optional Tesseract OCR language code (e.g. 'eng', 'eng+deu')",
    ),
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """Download content from a single URL with content negotiation, OCR, and ethical crawling."""
    active_settings = getattr(request.app.state, "settings", None) or settings
    try:
        validated_url = await validate_url_async(url, active_settings)
        parsed_actions = parse_actions_param(actions)
        validated_ocr_lang = (
            validate_ocr_language(ocr_lang, active_settings.ocr.default_language)
            if ocr_lang is not None
            else None
        )
        logger.info(f"Processing download request for: {validated_url}")

        if active_settings.crawling.respect_robots_txt:
            crawl_delay = await robots_checker.check_allowed(
                validated_url,
                active_settings.crawling.user_agent,
            )
            domain = urlparse(validated_url).netloc
            await domain_throttle.wait_if_needed(domain, crawl_delay)

        cached_http_result: tuple[bytes, ResponseMetadata] | None = None

        async def fetch_via_http() -> tuple[bytes, ResponseMetadata]:
            nonlocal cached_http_result
            if cached_http_result is None:
                cached_http_result = await http_client.download(validated_url, RequestPriority.HIGH)
            return cached_http_result

        remote_queue = (
            worker_queue
            if (
                active_settings.worker.deployment_mode == "api"
                and active_settings.worker.remote_browser_enabled
            )
            else None
        )
        remote_timeout = active_settings.worker.remote_browser_timeout

        if output == "pdf":
            return await handle_pdf_response(
                validated_url,
                None,
                pdf_semaphore,
                actions=parsed_actions or None,
                wait_for_selector=wait_for,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if output == "screenshot":
            return await handle_screenshot_response(
                validated_url,
                None,
                pdf_semaphore,
                screenshot_format=screenshot_format,
                full_page=full_page,
                wait_for_selector=wait_for,
                actions=parsed_actions or None,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        effective_render = render or (wait_for is not None) or bool(parsed_actions)
        formats = parse_accept_headers(request.headers.getlist("accept"))

        if len(formats) > 1:
            logger.info(f"Multi-format request: {formats} (Accept: {accept})")
            return await handle_multi_format_response(
                validated_url,
                None,
                None,
                formats,
                pdf_semaphore,
                effective_render,
                wait_for,
                http_fallback=fetch_via_http,
                actions=parsed_actions or None,
                full_page=full_page,
                ocr_language=validated_ocr_lang,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        format_type = parse_accept_header(accept)
        if ocr and format_type not in {"text", "markdown"}:
            format_type = "text"
        logger.info(f"Requested format: {format_type} (Accept: {accept})")

        if format_type == "pdf":
            return await handle_pdf_response(
                validated_url,
                None,
                pdf_semaphore,
                actions=parsed_actions or None,
                wait_for_selector=wait_for,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if format_type in {"screenshot", "screenshot_jpeg"}:
            shot_fmt: Literal["png", "jpeg"] = (
                "jpeg" if format_type == "screenshot_jpeg" else screenshot_format
            )
            return await handle_screenshot_response(
                validated_url,
                None,
                pdf_semaphore,
                screenshot_format=shot_fmt,
                full_page=full_page,
                wait_for_selector=wait_for,
                actions=parsed_actions or None,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        if effective_render and format_type in {"html", "text", "markdown"}:
            if format_type == "html":
                return await handle_html_response(
                    validated_url,
                    None,
                    None,
                    True,
                    wait_for,
                    pdf_semaphore=pdf_semaphore,
                    http_fallback=fetch_via_http,
                    actions=parsed_actions or None,
                    worker_queue=remote_queue,
                    remote_timeout=remote_timeout,
                )
            if format_type == "text":
                return await handle_text_response(
                    validated_url,
                    None,
                    None,
                    True,
                    wait_for,
                    pdf_semaphore=pdf_semaphore,
                    http_fallback=fetch_via_http,
                    actions=parsed_actions or None,
                    force_ocr=ocr,
                    ocr_language=validated_ocr_lang,
                    worker_queue=remote_queue,
                    remote_timeout=remote_timeout,
                )
            return await handle_markdown_response(
                validated_url,
                None,
                None,
                True,
                wait_for,
                pdf_semaphore=pdf_semaphore,
                http_fallback=fetch_via_http,
                actions=parsed_actions or None,
                force_ocr=ocr,
                ocr_language=validated_ocr_lang,
                worker_queue=remote_queue,
                remote_timeout=remote_timeout,
            )

        content, metadata = await fetch_via_http()

        if format_type == "json":
            return await handle_json_response(content, metadata)
        elif format_type == "text":
            return await handle_text_response(
                validated_url,
                content,
                metadata,
                pdf_semaphore=pdf_semaphore,
                force_ocr=ocr,
                ocr_language=validated_ocr_lang,
            )
        elif format_type == "markdown":
            return await handle_markdown_response(
                validated_url,
                content,
                metadata,
                pdf_semaphore=pdf_semaphore,
                force_ocr=ocr,
                ocr_language=validated_ocr_lang,
            )
        elif format_type == "html":
            return await handle_html_response(
                validated_url,
                content,
                metadata,
                False,
                wait_for,
                pdf_semaphore=pdf_semaphore,
            )
        else:
            return await handle_raw_response(content, metadata)

    except RobotsDisallowedError as e:
        logger.info(f"Blocked by robots.txt for {url}: {e}")
        raise HTTPException(
            status_code=403,
            detail=ErrorResponse(error=str(e), error_type="robots_disallowed").model_dump(),
        )

    except (URLValidationError, ValueError) as e:
        logger.warning(f"Request validation failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(e), error_type="validation_error").model_dump(),
        )

    except BrowserActionError as e:
        logger.warning(f"Browser action failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(e), error_type="browser_action_error").model_dump(),
        )

    except OCRUnavailableError as e:
        logger.warning(f"OCR unavailable for {url}: {e}")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(error=str(e), error_type="ocr_unavailable").model_dump(),
        )

    except OCRError as e:
        logger.warning(f"OCR processing failed for {url}: {e}")
        raise HTTPException(
            status_code=422,
            detail=ErrorResponse(error=str(e), error_type="ocr_error").model_dump(),
        )

    except ContentTooLargeError as e:
        logger.warning(f"Content size limit exceeded for {url}: {e}")
        raise HTTPException(
            status_code=413,
            detail=ErrorResponse(error=str(e), error_type="content_too_large").model_dump(),
        )

    except HTTPTimeoutError as e:
        logger.error(f"Timeout error for {url}: {e}")
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        )

    except TimeoutError as e:
        logger.error(f"Remote browser timeout for {url}: {e}")
        raise HTTPException(
            status_code=504,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        )

    except HTTPClientError as e:
        logger.error(f"HTTP client error for {url}: {e}")
        status_code = e.status_code if e.status_code else 502

        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
        )

    except DownloadError as e:
        logger.error(f"Download error for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(error=str(e), error_type="download_error").model_dump(),
        )

    except PDFGeneratorError as e:
        if e.status_code is not None and 400 <= e.status_code < 500:
            logger.warning(f"Upstream HTTP {e.status_code} during PDF generation for {url}: {e}")
            raise HTTPException(
                status_code=e.status_code,
                detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
            )
        logger.error(f"PDF generation failed for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=f"PDF generation failed: {e}",
                error_type="pdf_generation_error",
            ).model_dump(),
        )

    except SelectorTimeoutError as e:
        logger.warning(f"Selector timeout for {url}: {e}")
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="selector_timeout_error").model_dump(),
        )

    except HTTPException:
        raise

    except Exception as e:
        logger.exception(f"Unexpected error downloading {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error="Internal server error", error_type="internal_error"
            ).model_dump(),
        )

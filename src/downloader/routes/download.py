"""Single URL download endpoint."""

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response

from ..auth import get_api_key
from ..content_converter import SelectorTimeoutError
from ..crawling import RobotsDisallowedError
from ..dependencies import (
    DomainThrottleDep,
    HTTPClientDep,
    PDFSemaphoreDep,
    RobotsCheckerDep,
    SettingsDep,
)
from ..http_client import (
    ContentTooLargeError,
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
)
from ..models.responses import ErrorResponse, HTTPErrorResponse
from ..pdf_generator import PDFGeneratorError
from ..services.content_processor import (
    handle_html_response,
    handle_json_response,
    handle_markdown_response,
    handle_multi_format_response,
    handle_pdf_response,
    handle_raw_response,
    handle_text_response,
    parse_accept_header,
    parse_accept_headers,
)
from ..validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/{url:path}",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid URL or request"},
        403: {"model": HTTPErrorResponse, "description": "Disallowed by origin robots.txt"},
        408: {"model": HTTPErrorResponse, "description": "Request timeout"},
        413: {"model": HTTPErrorResponse, "description": "Response content exceeds size limit"},
        429: {"model": HTTPErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": HTTPErrorResponse, "description": "Internal server error"},
        502: {"model": HTTPErrorResponse, "description": "Upstream server error"},
        503: {"model": HTTPErrorResponse, "description": "Service temporarily unavailable"},
    },
)
async def download_url(
    request: Request,
    http_client: HTTPClientDep,
    pdf_semaphore: PDFSemaphoreDep,
    robots_checker: RobotsCheckerDep,
    domain_throttle: DomainThrottleDep,
    settings: SettingsDep,
    url: str = Path(..., description="The URL to download"),
    accept: str | None = Header(None, description="Accept header for content negotiation"),
    render: bool = Query(False, description="Force Playwright rendering, bypass auto-detection"),
    wait_for: str | None = Query(
        None,
        description="CSS selector to wait for before returning. Implies render=true.",
        max_length=500,
    ),
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """Download content from a single URL with content negotiation and ethical crawling."""
    try:
        validated_url = validate_url(url)
        logger.info(f"Processing download request for: {validated_url}")

        if settings.crawling.respect_robots_txt:
            crawl_delay = await robots_checker.check_allowed(
                validated_url,
                settings.crawling.user_agent,
            )
            domain = urlparse(validated_url).netloc
            await domain_throttle.wait_if_needed(domain, crawl_delay)

        content, metadata = await http_client.download(validated_url, RequestPriority.HIGH)

        effective_render = render or (wait_for is not None)
        formats = parse_accept_headers(request.headers.getlist("accept"))

        if len(formats) > 1:
            logger.info(f"Multi-format request: {formats} (Accept: {accept})")
            return await handle_multi_format_response(
                validated_url, content, metadata, formats, pdf_semaphore, effective_render, wait_for
            )
        else:
            format_type = parse_accept_header(accept)
            logger.info(f"Requested format: {format_type} (Accept: {accept})")

            if format_type == "json":
                return await handle_json_response(content, metadata)
            elif format_type == "text":
                return await handle_text_response(validated_url, content, metadata)
            elif format_type == "markdown":
                return await handle_markdown_response(validated_url, content, metadata)
            elif format_type == "pdf":
                return await handle_pdf_response(validated_url, metadata, pdf_semaphore)
            elif format_type == "html":
                return await handle_html_response(
                    validated_url, content, metadata, effective_render, wait_for
                )
            else:
                return await handle_raw_response(content, metadata)

    except RobotsDisallowedError as e:
        logger.info(f"Blocked by robots.txt for {url}: {e}")
        raise HTTPException(
            status_code=403,
            detail=ErrorResponse(error=str(e), error_type="robots_disallowed").model_dump(),
        )

    except URLValidationError as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(error=str(e), error_type="validation_error").model_dump(),
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

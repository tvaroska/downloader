"""Single URL download endpoint."""

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response

from ..auth import get_api_key
from ..http_client import (
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    RequestPriority,
    get_client,
)
from ..models.responses import ErrorResponse
from ..pdf_generator import PDFGeneratorError
from ..services.content_processor import (
    handle_html_response,
    handle_json_response,
    handle_markdown_response,
    handle_pdf_response,
    handle_raw_response,
    handle_text_response,
    parse_accept_header,
)
from ..validation import URLValidationError, validate_url

logger = logging.getLogger(__name__)

router = APIRouter()


def get_pdf_semaphore():
    """Get PDF semaphore from app state (to be injected)."""
    # This will be set by the main app
    from ..api import PDF_SEMAPHORE
    return PDF_SEMAPHORE


@router.get("/{url:path}")
async def download_url(
    url: str = Path(..., description="The URL to download"),
    accept: str | None = Header(None, description="Accept header for content negotiation"),
    request: Request = None,
    api_key: str | None = Depends(get_api_key),
) -> Response:
    """
    Download content from a single URL with content negotiation.

    Supports multiple output formats via Accept header:
    - text/plain: Plain text extraction
    - text/html: Original HTML
    - text/markdown: Markdown conversion
    - application/pdf: PDF generation
    - application/json: JSON with base64 content

    Args:
        url: URL to download
        accept: Accept header for format selection
        request: FastAPI request object
        api_key: API key for authentication (if enabled)

    Returns:
        Response in requested format

    Raises:
        HTTPException: For validation, timeout, or download errors
    """
    try:
        validated_url = validate_url(url)
        logger.info(f"Processing download request for: {validated_url}")

        client = await get_client()
        content, metadata = await client.download(validated_url, RequestPriority.HIGH)

        format_type = parse_accept_header(accept)
        logger.info(f"Requested format: {format_type} (Accept: {accept})")

        if format_type == "json":
            return await handle_json_response(content, metadata)
        elif format_type == "text":
            return await handle_text_response(validated_url, content, metadata)
        elif format_type == "markdown":
            return await handle_markdown_response(validated_url, content, metadata)
        elif format_type == "pdf":
            pdf_semaphore = get_pdf_semaphore()
            return await handle_pdf_response(validated_url, metadata, pdf_semaphore)
        elif format_type == "html":
            return await handle_html_response(content, metadata)
        else:
            return await handle_raw_response(content, metadata)

    except URLValidationError as e:
        logger.warning(f"URL validation failed for {url}: {e}")
        raise HTTPException(
            status_code=400,
            detail=ErrorResponse(
                error=str(e), error_type="validation_error"
            ).model_dump(),
        )

    except HTTPTimeoutError as e:
        logger.error(f"Timeout error for {url}: {e}")
        raise HTTPException(
            status_code=408,
            detail=ErrorResponse(error=str(e), error_type="timeout_error").model_dump(),
        )

    except HTTPClientError as e:
        logger.error(f"HTTP client error for {url}: {e}")
        if "404" in str(e):
            status_code = 404
        elif "403" in str(e):
            status_code = 403
        elif "401" in str(e):
            status_code = 401
        elif "500" in str(e):
            status_code = 502
        else:
            status_code = 502

        raise HTTPException(
            status_code=status_code,
            detail=ErrorResponse(error=str(e), error_type="http_error").model_dump(),
        )

    except DownloadError as e:
        logger.error(f"Download error for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=str(e), error_type="download_error"
            ).model_dump(),
        )

    except PDFGeneratorError as e:
        logger.error(f"PDF generation failed for {url}: {e}")
        raise HTTPException(
            status_code=500,
            detail=ErrorResponse(
                error=f"PDF generation failed: {e}", error_type="pdf_generation_error"
            ).model_dump(),
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

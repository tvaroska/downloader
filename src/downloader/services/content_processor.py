"""Content processing service for handling downloads and format conversions."""

import asyncio
import base64
import json
import logging
import time

from fastapi import HTTPException, Response

from ..content_converter import (
    SelectorTimeoutError,
    convert_content_to_markdown,
    convert_content_to_text,
    convert_content_with_playwright_fallback,
    render_html_with_playwright,
    should_use_playwright_fallback,
    should_use_playwright_for_html,
)
from ..metrics import (
    record_html_rendering_detection,
    record_html_rendering_duration,
    record_html_rendering_failure,
    record_html_rendering_success,
)
from ..models.responses import ErrorResponse, ResponseMetadata
from ..pdf_generator import generate_pdf_from_url

logger = logging.getLogger(__name__)


def _format_to_mime_type(format_type: str) -> str:
    """
    Convert internal format string to MIME type.

    Args:
        format_type: Internal format string ('text', 'html', etc.)

    Returns:
        MIME type string
    """
    mapping = {
        "text": "text/plain",
        "html": "text/html",
        "markdown": "text/markdown",
        "pdf": "application/pdf",
        "json": "application/json",
    }
    return mapping.get(format_type, "application/octet-stream")


def parse_accept_header(accept_header: str | None) -> str:
    """
    Parse Accept header to determine response format.

    Args:
        accept_header: The Accept header value

    Returns:
        Format string: 'text', 'html', 'markdown', 'pdf', 'json', or 'raw'
    """
    if not accept_header:
        return "text"

    accept_header = accept_header.lower()

    if "text/plain" in accept_header:
        return "text"
    elif "text/html" in accept_header:
        return "html"
    elif "text/markdown" in accept_header or "text/x-markdown" in accept_header:
        return "markdown"
    elif "application/pdf" in accept_header:
        return "pdf"
    elif "application/json" in accept_header:
        return "json"
    else:
        return "raw"


def parse_accept_headers(accept_headers: str | list[str] | None) -> list[str]:
    """
    Parse Accept header(s) to determine all requested response formats.

    Supports:
    - Single Accept header with comma-separated values: "text/html, text/markdown"
    - Multiple Accept header values
    - Quality parameters (ignored but parsed for compatibility)

    Args:
        accept_headers: Accept header value(s) from FastAPI

    Returns:
        List of format strings: ['html', 'markdown', 'pdf', etc.]
        Empty list if no valid formats found
    """
    if not accept_headers:
        return []

    # Normalize input to list
    if isinstance(accept_headers, str):
        header_list = [accept_headers]
    else:
        header_list = accept_headers

    # Parse all media types
    media_types = []
    for header in header_list:
        # Split by comma for comma-separated values
        parts = header.split(",")
        for part in parts:
            # Strip whitespace and quality parameters
            media_type = part.strip().split(";")[0].strip().lower()
            if media_type:
                media_types.append(media_type)

    # Map media types to internal format strings
    formats = []
    for media_type in media_types:
        if media_type == "text/plain":
            formats.append("text")
        elif media_type == "text/html":
            formats.append("html")
        elif media_type in ("text/markdown", "text/x-markdown"):
            formats.append("markdown")
        elif media_type == "application/pdf":
            formats.append("pdf")
        elif media_type == "application/json":
            formats.append("json")
        # Ignore unsupported formats

    # Deduplicate while preserving order
    seen = set()
    unique_formats = []
    for fmt in formats:
        if fmt not in seen:
            seen.add(fmt)
            unique_formats.append(fmt)

    return unique_formats


async def _playwright_fallback_for_content(
    url: str,
    processed_content: str,
    original_content: bytes,
    content_type: str,
    output_format: str,
    request_id: str = "",
) -> str:
    """
    Apply Playwright fallback for empty HTML content.

    Args:
        url: URL being processed
        processed_content: Initially processed content
        original_content: Original content bytes
        content_type: MIME type
        output_format: Target format ('text' or 'markdown')
        request_id: Request identifier for logging

    Returns:
        Enhanced content from Playwright or original content
    """
    if not processed_content.strip() and should_use_playwright_fallback(
        url, original_content, content_type
    ):
        logger.info(
            f"[{request_id}] Triggering Playwright {output_format} fallback for empty content"
        )
        try:
            fallback_start_time = asyncio.get_event_loop().time()
            fallback_content = await convert_content_with_playwright_fallback(url, output_format)
            fallback_duration = asyncio.get_event_loop().time() - fallback_start_time
            logger.info(
                f"✅ Playwright {output_format} fallback successful for {url}: "
                f"{len(fallback_content)} characters in {fallback_duration:.2f}s"
            )
            return fallback_content
        except Exception as e:
            logger.error(f"❌ Playwright {output_format} fallback failed for {url}: {str(e)}")
            logger.info(f"Falling back to original empty {output_format} for {url}")

    return processed_content


async def handle_json_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """Handle JSON format response with base64-encoded content."""
    content_b64 = base64.b64encode(content).decode("utf-8")

    json_response = {
        "success": True,
        "url": metadata["url"],
        "size": metadata["size"],
        "content_type": metadata["content_type"],
        "content": content_b64,
        "metadata": metadata,
    }

    return Response(
        content=json.dumps(json_response),
        media_type="application/json",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def handle_text_response(
    validated_url: str, content: bytes, metadata: ResponseMetadata
) -> Response:
    """Handle plain text format response."""
    text_content = convert_content_to_text(content, metadata["content_type"])

    if "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 text extraction for {validated_url}: {len(text_content)} characters extracted"
        )

        text_content = await _playwright_fallback_for_content(
            validated_url,
            text_content,
            content,
            metadata["content_type"],
            "text",
        )

        if text_content.strip():
            logger.debug(f"Successfully extracted content for {validated_url}")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping fallback logic")

    return Response(
        content=text_content,
        media_type="text/plain; charset=utf-8",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def handle_markdown_response(
    validated_url: str, content: bytes, metadata: ResponseMetadata
) -> Response:
    """Handle markdown format response."""
    markdown_content = convert_content_to_markdown(content, metadata["content_type"])

    if "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 markdown extraction for {validated_url}: {len(markdown_content)} characters extracted"
        )

        markdown_content = await _playwright_fallback_for_content(
            validated_url,
            markdown_content,
            content,
            metadata["content_type"],
            "markdown",
        )

        if markdown_content.strip():
            logger.debug(f"Successfully extracted markdown for {validated_url}")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping markdown fallback logic")

    return Response(
        content=markdown_content,
        media_type="text/markdown; charset=utf-8",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
        },
    )


async def handle_pdf_response(
    validated_url: str,
    metadata: ResponseMetadata,
    pdf_semaphore: asyncio.Semaphore,
) -> Response:
    """Handle PDF format response with concurrency control."""
    logger.info(f"Generating PDF for: {validated_url}")

    if pdf_semaphore.locked():
        logger.warning(f"PDF service at capacity, rejecting request for: {validated_url}")
        raise HTTPException(
            status_code=503,
            detail=ErrorResponse(
                error="PDF service temporarily unavailable. Please try again later.",
                error_type="service_unavailable",
            ).model_dump(),
        )

    async with pdf_semaphore:
        pdf_content = await generate_pdf_from_url(validated_url)

    return Response(
        content=pdf_content,
        media_type="application/pdf",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "Content-Length": str(len(pdf_content)),
            "Content-Disposition": 'inline; filename="download.pdf"',
        },
    )


async def handle_html_response(
    validated_url: str,
    content: bytes,
    metadata: ResponseMetadata,
    force_render: bool = False,
    wait_for_selector: str | None = None,
) -> Response:
    """
    Handle HTML format response with optional Playwright rendering.

    Flow:
    1. Check if forced rendering via ?render=true, or auto-detect JS-heavy content
    2. If yes, fetch rendered HTML using Playwright
    3. Return HTML content with appropriate headers

    Args:
        validated_url: The validated URL being processed
        content: Raw HTML content bytes
        metadata: Response metadata
        force_render: If True, bypass auto-detection and force Playwright rendering
        wait_for_selector: Optional CSS selector to wait for after page load
    """
    rendered_with_js = False

    if "html" in metadata["content_type"].lower():
        # Check if we need to render with Playwright (forced or auto-detected)
        needs_render = force_render or should_use_playwright_for_html(
            validated_url, content, metadata["content_type"]
        )
        if needs_render:
            render_reason = "forced via ?render=true" if force_render else "auto-detected JS-heavy"
            logger.info(f"Playwright rendering ({render_reason}) for {validated_url}")
            record_html_rendering_detection()

            try:
                # Render HTML with Playwright
                start_time = time.time()
                rendered_html = await render_html_with_playwright(validated_url, wait_for_selector)
                duration = time.time() - start_time

                logger.info(
                    f"✅ Playwright HTML rendering successful: "
                    f"{len(content)} bytes → {len(rendered_html)} bytes "
                    f"({(len(rendered_html) / len(content) * 100):.1f}% size increase) "
                    f"in {duration:.2f}s"
                )

                # Record metrics
                record_html_rendering_duration(duration)
                record_html_rendering_success(len(content), len(rendered_html))

                # Use rendered HTML instead of raw
                html_content = rendered_html
                rendered_with_js = True

            except SelectorTimeoutError:
                # Selector timeout should not degrade gracefully - re-raise
                raise
            except Exception as e:
                # Graceful degradation - log error and use raw HTML
                logger.error(f"❌ Playwright HTML rendering failed for {validated_url}: {str(e)}")
                logger.info(f"Falling back to raw HTML for {validated_url}")
                record_html_rendering_failure()
                html_content = content
        else:
            # Use raw HTML for static pages
            logger.debug(f"Using raw HTML (no JS rendering needed) for {validated_url}")
            html_content = content

        response_content_type = metadata["content_type"]
    else:
        # Non-HTML content - convert to HTML
        html_content = content.decode("utf-8", errors="ignore")
        response_content_type = "text/html; charset=utf-8"

    return Response(
        content=html_content,
        media_type=response_content_type,
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
            "X-Rendered-With-JS": "true" if rendered_with_js else "false",
        },
    )


async def handle_raw_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """Handle raw format response (default)."""
    return Response(
        content=content,
        media_type=metadata.get("content_type", "application/octet-stream"),
        headers={
            "X-Original-URL": metadata["url"],
            "Content-Length": str(metadata["size"]),
        },
    )


async def _process_single_format_for_multi(
    format_type: str,
    url: str,
    content: bytes,
    metadata: ResponseMetadata,
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
) -> tuple[str, str]:
    """
    Process a single format for multi-format response.

    Args:
        format_type: Internal format string ('text', 'html', etc.)
        url: URL being processed
        content: Downloaded content bytes
        metadata: Response metadata
        pdf_semaphore: Semaphore for PDF generation
        force_render: If True, force Playwright rendering for HTML
        wait_for_selector: Optional CSS selector to wait for after page load

    Returns:
        Tuple of (mime_type, processed_content)

    Raises:
        Exception with descriptive error message on failure
    """
    try:
        if format_type == "json":
            response = await handle_json_response(content, metadata)
            content_str = response.body.decode("utf-8")
            return ("application/json", content_str)

        elif format_type == "text":
            response = await handle_text_response(url, content, metadata)
            content_str = response.body.decode("utf-8")
            return ("text/plain", content_str)

        elif format_type == "markdown":
            response = await handle_markdown_response(url, content, metadata)
            content_str = response.body.decode("utf-8")
            return ("text/markdown", content_str)

        elif format_type == "pdf":
            # Check semaphore availability
            if pdf_semaphore.locked():
                raise Exception("PDF service at capacity")
            response = await handle_pdf_response(url, metadata, pdf_semaphore)
            # Base64 encode for JSON
            pdf_bytes = response.body
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
            return ("application/pdf", pdf_base64)

        elif format_type == "html":
            response = await handle_html_response(
                url, content, metadata, force_render, wait_for_selector
            )
            if isinstance(response.body, bytes):
                content_str = response.body.decode("utf-8", errors="ignore")
            else:
                content_str = response.body
            return ("text/html", content_str)

        else:
            raise Exception(f"Unsupported format: {format_type}")

    except Exception as e:
        # Re-raise with format context
        raise Exception(f"{format_type} processing failed: {str(e)}")


async def process_multiple_formats(
    url: str,
    content: bytes,
    metadata: ResponseMetadata,
    formats: list[str],
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
) -> dict[str, str]:
    """
    Process content into multiple formats in parallel.

    Args:
        url: URL being processed
        content: Downloaded content bytes
        metadata: Response metadata
        formats: List of format strings to generate
        pdf_semaphore: Semaphore for PDF generation
        force_render: If True, force Playwright rendering for HTML
        wait_for_selector: Optional CSS selector to wait for after page load

    Returns:
        Dict structure:
        {
            "text/html": "<html content>",
            "text/markdown": "# markdown",
            "errors": {
                "application/pdf": "Error message"
            }
        }
    """
    # Create tasks for each format
    tasks = []
    format_types = []

    for format_type in formats:
        task = asyncio.create_task(
            _process_single_format_for_multi(
                format_type, url, content, metadata, pdf_semaphore, force_render, wait_for_selector
            )
        )
        tasks.append(task)
        format_types.append(format_type)

    # Execute in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Build response dict
    response_dict = {}
    errors = {}

    for format_type, result in zip(format_types, results, strict=False):
        if isinstance(result, Exception):
            # Format failed - add to errors
            mime_type = _format_to_mime_type(format_type)
            errors[mime_type] = str(result)
        else:
            # Format succeeded - add to results
            mime_type, content_str = result
            response_dict[mime_type] = content_str

    # Add errors dict if any errors occurred
    if errors:
        response_dict["errors"] = errors

    return response_dict


async def handle_multi_format_response(
    url: str,
    content: bytes,
    metadata: ResponseMetadata,
    formats: list[str],
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
) -> Response:
    """
    Create multi-format JSON response.

    Args:
        url: URL being processed
        content: Downloaded content bytes
        metadata: Response metadata
        formats: List of format strings to generate
        pdf_semaphore: Semaphore for PDF generation
        force_render: If True, force Playwright rendering for HTML
        wait_for_selector: Optional CSS selector to wait for after page load

    Returns:
        JSON Response with all requested formats
    """
    results_dict = await process_multiple_formats(
        url, content, metadata, formats, pdf_semaphore, force_render, wait_for_selector
    )

    return Response(
        content=json.dumps(results_dict),
        media_type="application/json",
        headers={
            "X-Original-URL": metadata["url"],
            "X-Multi-Format": "true",
            "X-Requested-Formats": ",".join(formats),
        },
    )

"""Content processing service for handling downloads and format conversions."""

import asyncio
import base64
import json
import logging

from fastapi import HTTPException, Response

from ..content_converter import (
    convert_content_to_markdown,
    convert_content_to_text,
    convert_content_with_playwright_fallback,
    should_use_playwright_fallback,
)
from ..models.responses import ErrorResponse, ResponseMetadata
from ..pdf_generator import generate_pdf_from_url

logger = logging.getLogger(__name__)


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


async def handle_html_response(content: bytes, metadata: ResponseMetadata) -> Response:
    """Handle HTML format response."""
    if "html" in metadata["content_type"].lower():
        html_content = content
        response_content_type = metadata["content_type"]
    else:
        html_content = content.decode("utf-8", errors="ignore")
        response_content_type = "text/html; charset=utf-8"

    return Response(
        content=html_content,
        media_type=response_content_type,
        headers={
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": metadata["content_type"],
            "X-Content-Length": str(metadata["size"]),
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

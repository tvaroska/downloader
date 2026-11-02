"""Content processing service for handling downloads and format conversions."""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

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

HTTPFallbackFetcher = Callable[[], Awaitable[tuple[bytes, ResponseMetadata]]]


@asynccontextmanager
async def _acquire_browser_slot(
    pdf_semaphore: asyncio.Semaphore | None,
    *,
    fail_fast: bool,
    url: str,
) -> AsyncIterator[bool]:
    if pdf_semaphore is None:
        yield True
        return

    if pdf_semaphore.locked():
        if fail_fast:
            logger.warning(f"Browser rendering service at capacity, rejecting request for: {url}")
            raise HTTPException(
                status_code=503,
                detail=ErrorResponse(
                    error="Browser rendering service temporarily unavailable. Please try again later.",
                    error_type="service_unavailable",
                ).model_dump(),
            )
        logger.info(f"Browser semaphore saturated, skipping auto-render fallback for {url}")
        yield False
        return

    async with pdf_semaphore:
        yield True


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

    if isinstance(accept_headers, str):
        header_list = [accept_headers]
    else:
        header_list = accept_headers

    media_types = []
    for header in header_list:
        parts = header.split(",")
        for part in parts:
            media_type = part.strip().split(";")[0].strip().lower()
            if media_type:
                media_types.append(media_type)

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
    output_format: Literal["text", "markdown"],
    request_id: str = "",
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
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
        async with _acquire_browser_slot(pdf_semaphore, fail_fast=False, url=url) as acquired:
            if not acquired:
                return processed_content

            logger.info(
                f"[{request_id}] Triggering Playwright {output_format} fallback for empty content"
            )
            try:
                fallback_start_time = asyncio.get_event_loop().time()
                fallback_content = await convert_content_with_playwright_fallback(
                    url, output_format
                )
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


async def _render_html_once_with_semaphore(
    validated_url: str,
    wait_for_selector: str | None,
    pdf_semaphore: asyncio.Semaphore | None,
    raw_size_hint: int | None = None,
) -> bytes:
    record_html_rendering_detection()
    async with _acquire_browser_slot(pdf_semaphore, fail_fast=True, url=validated_url):
        start_time = time.time()
        rendered_html = await render_html_with_playwright(validated_url, wait_for_selector)
        duration = time.time() - start_time
        orig_len = raw_size_hint if raw_size_hint is not None else len(rendered_html)
        pct = (len(rendered_html) / max(orig_len, 1)) * 100
        logger.info(
            f"✅ Playwright HTML rendering successful: "
            f"{orig_len} bytes → {len(rendered_html)} bytes "
            f"({pct:.1f}% size increase) in {duration:.2f}s"
        )
        record_html_rendering_duration(duration)
        record_html_rendering_success(orig_len, len(rendered_html))
        return rendered_html


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
    validated_url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
    http_fallback: HTTPFallbackFetcher | None = None,
    already_rendered: bool = False,
) -> Response:
    """Handle plain text format response."""
    if already_rendered:
        assert content is not None
        orig_url = metadata["url"] if metadata is not None else validated_url
        orig_ct = metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
        orig_len = str(metadata["size"] if metadata is not None else len(content))
        text_content = convert_content_to_text(content, "text/html")
        return Response(
            content=text_content,
            media_type="text/plain; charset=utf-8",
            headers={
                "X-Original-URL": orig_url,
                "X-Original-Content-Type": orig_ct,
                "X-Content-Length": orig_len,
                "X-Rendered-With-JS": "true",
            },
        )

    if force_render and (
        content is None or (metadata is not None and "html" in metadata["content_type"].lower())
    ):
        try:
            rendered_html = await _render_html_once_with_semaphore(
                validated_url,
                wait_for_selector,
                pdf_semaphore,
                len(content) if content is not None else None,
            )
            orig_url = metadata["url"] if metadata is not None else validated_url
            orig_ct = (
                metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
            )
            orig_len = str(metadata["size"] if metadata is not None else len(rendered_html))
            text_content = convert_content_to_text(rendered_html, "text/html")
            return Response(
                content=text_content,
                media_type="text/plain; charset=utf-8",
                headers={
                    "X-Original-URL": orig_url,
                    "X-Original-Content-Type": orig_ct,
                    "X-Content-Length": orig_len,
                    "X-Rendered-With-JS": "true",
                },
            )
        except (SelectorTimeoutError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Playwright text rendering failed for {validated_url}: {e}")
            record_html_rendering_failure()
            if content is None or metadata is None:
                if http_fallback is None:
                    raise
                content, metadata = await http_fallback()
            text_content = convert_content_to_text(content, metadata["content_type"])
            return Response(
                content=text_content,
                media_type="text/plain; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                    "X-Rendered-With-JS": "false",
                },
            )

    if content is None or metadata is None:
        if http_fallback is None:
            raise ValueError("content and metadata or http_fallback must be provided")
        content, metadata = await http_fallback()

    text_content = convert_content_to_text(content, metadata["content_type"])

    if not force_render and "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 text extraction for {validated_url}: {len(text_content)} characters extracted"
        )

        text_content = await _playwright_fallback_for_content(
            validated_url,
            text_content,
            content,
            metadata["content_type"],
            "text",
            pdf_semaphore=pdf_semaphore,
        )

        if text_content.strip():
            logger.debug(f"Successfully extracted content for {validated_url}")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping fallback logic")

    headers = {
        "X-Original-URL": metadata["url"],
        "X-Original-Content-Type": metadata["content_type"],
        "X-Content-Length": str(metadata["size"]),
    }
    if force_render:
        headers["X-Rendered-With-JS"] = "false"

    return Response(
        content=text_content,
        media_type="text/plain; charset=utf-8",
        headers=headers,
    )


async def handle_markdown_response(
    validated_url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
    http_fallback: HTTPFallbackFetcher | None = None,
    already_rendered: bool = False,
) -> Response:
    """Handle markdown format response."""
    if already_rendered:
        assert content is not None
        orig_url = metadata["url"] if metadata is not None else validated_url
        orig_ct = metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
        orig_len = str(metadata["size"] if metadata is not None else len(content))
        markdown_content = convert_content_to_markdown(content, "text/html")
        return Response(
            content=markdown_content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "X-Original-URL": orig_url,
                "X-Original-Content-Type": orig_ct,
                "X-Content-Length": orig_len,
                "X-Rendered-With-JS": "true",
            },
        )

    if force_render and (
        content is None or (metadata is not None and "html" in metadata["content_type"].lower())
    ):
        try:
            rendered_html = await _render_html_once_with_semaphore(
                validated_url,
                wait_for_selector,
                pdf_semaphore,
                len(content) if content is not None else None,
            )
            orig_url = metadata["url"] if metadata is not None else validated_url
            orig_ct = (
                metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
            )
            orig_len = str(metadata["size"] if metadata is not None else len(rendered_html))
            markdown_content = convert_content_to_markdown(rendered_html, "text/html")
            return Response(
                content=markdown_content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "X-Original-URL": orig_url,
                    "X-Original-Content-Type": orig_ct,
                    "X-Content-Length": orig_len,
                    "X-Rendered-With-JS": "true",
                },
            )
        except (SelectorTimeoutError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Playwright markdown rendering failed for {validated_url}: {e}")
            record_html_rendering_failure()
            if content is None or metadata is None:
                if http_fallback is None:
                    raise
                content, metadata = await http_fallback()
            markdown_content = convert_content_to_markdown(content, metadata["content_type"])
            return Response(
                content=markdown_content,
                media_type="text/markdown; charset=utf-8",
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                    "X-Rendered-With-JS": "false",
                },
            )

    if content is None or metadata is None:
        if http_fallback is None:
            raise ValueError("content and metadata or http_fallback must be provided")
        content, metadata = await http_fallback()

    markdown_content = convert_content_to_markdown(content, metadata["content_type"])

    if not force_render and "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 markdown extraction for {validated_url}: {len(markdown_content)} characters extracted"
        )

        markdown_content = await _playwright_fallback_for_content(
            validated_url,
            markdown_content,
            content,
            metadata["content_type"],
            "markdown",
            pdf_semaphore=pdf_semaphore,
        )

        if markdown_content.strip():
            logger.debug(f"Successfully extracted markdown for {validated_url}")
    else:
        logger.debug(f"Non-HTML content for {validated_url}, skipping markdown fallback logic")

    headers = {
        "X-Original-URL": metadata["url"],
        "X-Original-Content-Type": metadata["content_type"],
        "X-Content-Length": str(metadata["size"]),
    }
    if force_render:
        headers["X-Rendered-With-JS"] = "false"

    return Response(
        content=markdown_content,
        media_type="text/markdown; charset=utf-8",
        headers=headers,
    )


async def handle_pdf_response(
    validated_url: str,
    metadata: ResponseMetadata | None,
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
            "X-Original-URL": metadata["url"] if metadata is not None else validated_url,
            "X-Original-Content-Type": (
                metadata["content_type"] if metadata is not None else "text/html"
            ),
            "Content-Length": str(len(pdf_content)),
            "Content-Disposition": 'inline; filename="download.pdf"',
        },
    )


async def handle_html_response(
    validated_url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
    http_fallback: HTTPFallbackFetcher | None = None,
    already_rendered: bool = False,
) -> Response:
    """
    Handle HTML format response with optional Playwright rendering.

    Args:
        validated_url: The validated URL being processed
        content: Raw HTML content bytes (or None when single-fetch rendering is requested)
        metadata: Response metadata (or None when single-fetch rendering is requested)
        force_render: If True, bypass auto-detection and force Playwright rendering
        wait_for_selector: Optional CSS selector to wait for after page load
    """
    if already_rendered:
        assert content is not None
        orig_url = metadata["url"] if metadata is not None else validated_url
        orig_ct = metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
        orig_len = str(metadata["size"] if metadata is not None else len(content))
        return Response(
            content=content,
            media_type=orig_ct,
            headers={
                "X-Original-URL": orig_url,
                "X-Original-Content-Type": orig_ct,
                "X-Content-Length": orig_len,
                "X-Rendered-With-JS": "true",
            },
        )

    if force_render and (
        content is None or (metadata is not None and "html" in metadata["content_type"].lower())
    ):
        logger.info(f"Playwright rendering (forced via ?render=true) for {validated_url}")
        try:
            rendered_html = await _render_html_once_with_semaphore(
                validated_url,
                wait_for_selector,
                pdf_semaphore,
                len(content) if content is not None else None,
            )
            orig_url = metadata["url"] if metadata is not None else validated_url
            orig_ct = (
                metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
            )
            orig_len = str(metadata["size"] if metadata is not None else len(rendered_html))
            return Response(
                content=rendered_html,
                media_type=orig_ct,
                headers={
                    "X-Original-URL": orig_url,
                    "X-Original-Content-Type": orig_ct,
                    "X-Content-Length": orig_len,
                    "X-Rendered-With-JS": "true",
                },
            )
        except (SelectorTimeoutError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Playwright HTML rendering failed for {validated_url}: {str(e)}")
            logger.info(f"Falling back to raw HTML for {validated_url}")
            record_html_rendering_failure()
            if content is None or metadata is None:
                if http_fallback is None:
                    raise
                content, metadata = await http_fallback()
            if "html" in metadata["content_type"].lower():
                degraded_body: bytes | str = content
                resp_ct = metadata["content_type"]
            else:
                degraded_body = content.decode("utf-8", errors="ignore")
                resp_ct = "text/html; charset=utf-8"
            return Response(
                content=degraded_body,
                media_type=resp_ct,
                headers={
                    "X-Original-URL": metadata["url"],
                    "X-Original-Content-Type": metadata["content_type"],
                    "X-Content-Length": str(metadata["size"]),
                    "X-Rendered-With-JS": "false",
                },
            )

    if content is None or metadata is None:
        if http_fallback is None:
            raise ValueError("content and metadata or http_fallback must be provided")
        content, metadata = await http_fallback()

    rendered_with_js = False
    html_content: bytes | str

    if "html" in metadata["content_type"].lower():
        if not force_render and should_use_playwright_for_html(
            validated_url, content, metadata["content_type"]
        ):
            logger.info(f"Playwright rendering (auto-detected JS-heavy) for {validated_url}")
            async with _acquire_browser_slot(
                pdf_semaphore, fail_fast=False, url=validated_url
            ) as acquired:
                if acquired:
                    record_html_rendering_detection()
                    try:
                        start_time = time.time()
                        rendered_html = await render_html_with_playwright(
                            validated_url, wait_for_selector
                        )
                        duration = time.time() - start_time
                        pct = (len(rendered_html) / max(len(content), 1)) * 100
                        logger.info(
                            f"✅ Playwright HTML rendering successful: "
                            f"{len(content)} bytes → {len(rendered_html)} bytes "
                            f"({pct:.1f}% size increase) in {duration:.2f}s"
                        )
                        record_html_rendering_duration(duration)
                        record_html_rendering_success(len(content), len(rendered_html))
                        html_content = rendered_html
                        rendered_with_js = True
                    except SelectorTimeoutError:
                        raise
                    except Exception as e:
                        logger.error(
                            f"❌ Playwright HTML rendering failed for {validated_url}: {str(e)}"
                        )
                        logger.info(f"Falling back to raw HTML for {validated_url}")
                        record_html_rendering_failure()
                        html_content = content
                else:
                    html_content = content
        else:
            logger.debug(f"Using raw HTML (no JS rendering needed) for {validated_url}")
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
    content: bytes | None,
    metadata: ResponseMetadata | None,
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    already_rendered: bool = False,
    degraded_force_render: bool = False,
) -> tuple[str, str]:
    """Process a single format for multi-format response."""
    try:
        if format_type == "json":
            if content is None or metadata is None:
                raise Exception("Raw content required for JSON format")
            response = await handle_json_response(content, metadata)
            content_str = bytes(response.body).decode("utf-8")
            return ("application/json", content_str)

        elif format_type == "text":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded text extraction")
                return ("text/plain", convert_content_to_text(content, metadata["content_type"]))
            response = await handle_text_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
            )
            content_str = bytes(response.body).decode("utf-8")
            return ("text/plain", content_str)

        elif format_type == "markdown":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded markdown extraction")
                return (
                    "text/markdown",
                    convert_content_to_markdown(content, metadata["content_type"]),
                )
            response = await handle_markdown_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
            )
            content_str = bytes(response.body).decode("utf-8")
            return ("text/markdown", content_str)

        elif format_type == "pdf":
            if pdf_semaphore.locked():
                raise Exception("PDF service at capacity")
            response = await handle_pdf_response(url, metadata, pdf_semaphore)
            pdf_bytes = bytes(response.body)
            pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")
            return ("application/pdf", pdf_base64)

        elif format_type == "html":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded HTML response")
                return ("text/html", content.decode("utf-8", errors="ignore"))
            response = await handle_html_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
            )
            content_str = bytes(response.body).decode("utf-8", errors="ignore")
            return ("text/html", content_str)

        else:
            raise Exception(f"Unsupported format: {format_type}")

    except Exception as e:
        raise Exception(f"{format_type} processing failed: {str(e)}")


async def process_multiple_formats(
    url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    formats: list[str],
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    http_fallback: HTTPFallbackFetcher | None = None,
) -> dict[str, Any]:
    """Process content into multiple formats in parallel."""
    rendered_dom: bytes | None = None
    rendered_meta: ResponseMetadata | None = None
    degraded_force_render = False

    can_render = content is None or (
        metadata is not None and "html" in metadata["content_type"].lower()
    )
    if force_render and can_render and any(fmt in {"html", "text", "markdown"} for fmt in formats):
        try:
            rendered_dom = await _render_html_once_with_semaphore(
                url,
                wait_for_selector,
                pdf_semaphore,
                len(content) if content is not None else None,
            )
            rendered_meta = metadata or {
                "status_code": 200,
                "headers": {"content-type": "text/html; charset=utf-8"},
                "url": url,
                "size": len(rendered_dom),
                "content_type": "text/html; charset=utf-8",
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            }
        except (SelectorTimeoutError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Multi-format Playwright rendering failed for {url}: {e}")
            record_html_rendering_failure()
            degraded_force_render = True
            if (content is None or metadata is None) and http_fallback is not None:
                content, metadata = await http_fallback()

    needs_http_fetch = any(fmt == "json" for fmt in formats) or (
        not force_render and any(fmt != "pdf" for fmt in formats)
    )
    if needs_http_fetch and (content is None or metadata is None) and http_fallback is not None:
        content, metadata = await http_fallback()

    tasks = []
    format_types = []

    for format_type in formats:
        if format_type in {"html", "text", "markdown"} and rendered_dom is not None:
            task = asyncio.create_task(
                _process_single_format_for_multi(
                    format_type,
                    url,
                    rendered_dom,
                    rendered_meta,
                    pdf_semaphore,
                    False,
                    None,
                    already_rendered=True,
                )
            )
        elif format_type in {"html", "text", "markdown"} and degraded_force_render:
            task = asyncio.create_task(
                _process_single_format_for_multi(
                    format_type,
                    url,
                    content,
                    metadata,
                    pdf_semaphore,
                    False,
                    None,
                    degraded_force_render=True,
                )
            )
        else:
            task = asyncio.create_task(
                _process_single_format_for_multi(
                    format_type,
                    url,
                    content,
                    metadata,
                    pdf_semaphore,
                    force_render,
                    wait_for_selector,
                )
            )
        tasks.append(task)
        format_types.append(format_type)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    response_dict: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for format_type, result in zip(format_types, results, strict=False):
        if isinstance(result, Exception):
            mime_type = _format_to_mime_type(format_type)
            errors[mime_type] = str(result)
        elif isinstance(result, BaseException):
            raise result
        else:
            mime_type, content_str = result
            response_dict[mime_type] = content_str

    if errors:
        response_dict["errors"] = errors

    return response_dict


async def handle_multi_format_response(
    url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    formats: list[str],
    pdf_semaphore: asyncio.Semaphore,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    http_fallback: HTTPFallbackFetcher | None = None,
) -> Response:
    """Create multi-format JSON response."""
    results_dict = await process_multiple_formats(
        url,
        content,
        metadata,
        formats,
        pdf_semaphore,
        force_render,
        wait_for_selector,
        http_fallback=http_fallback,
    )

    return Response(
        content=json.dumps(results_dict),
        media_type="application/json",
        headers={
            "X-Original-URL": metadata["url"] if metadata is not None else url,
            "X-Multi-Format": "true",
            "X-Requested-Formats": ",".join(formats),
        },
    )

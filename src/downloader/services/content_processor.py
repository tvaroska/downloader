"""Content processing service for handling downloads, OCR, browser capture, and format conversion."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, Response

from ..browser.actions import BrowserAction, BrowserActionError
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
from ..transformers.ocr import (
    extract_text_from_image,
    format_ocr_markdown,
    is_ocr_image_content_type,
)
from ..validation import URLValidationError
from .content_negotiation import (
    _format_to_mime_type,
    handle_json_response,
    handle_raw_response,
    handle_screenshot_response,
    parse_accept_header,
    parse_accept_headers,
    run_remote_browser_task,
)
from .multi_format import handle_multi_format_response, process_multiple_formats

if TYPE_CHECKING:
    from ..worker_queue import WorkerQueue

logger = logging.getLogger(__name__)

HTTPFallbackFetcher = Callable[[], Awaitable[tuple[bytes, ResponseMetadata]]]

__all__ = [
    "HTTPFallbackFetcher",
    "_acquire_browser_slot",
    "_format_to_mime_type",
    "_playwright_fallback_for_content",
    "_render_html_once_with_semaphore",
    "handle_html_response",
    "handle_json_response",
    "handle_markdown_response",
    "handle_multi_format_response",
    "handle_pdf_response",
    "handle_raw_response",
    "handle_screenshot_response",
    "handle_text_response",
    "parse_accept_header",
    "parse_accept_headers",
    "process_multiple_formats",
]


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
    """Apply Playwright fallback for empty HTML content."""
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
                logger.error(f"❌ Playwright {output_format} fallback failed for {url}: {e}")
                logger.info(f"Falling back to original empty {output_format} for {url}")

    return processed_content


async def _render_html_once_with_semaphore(
    validated_url: str,
    wait_for_selector: str | None,
    pdf_semaphore: asyncio.Semaphore | None,
    raw_size_hint: int | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> bytes:
    record_html_rendering_detection()
    if worker_queue is not None:
        start_time = time.time()
        rendered_html = await run_remote_browser_task(
            worker_queue,
            kind="render_html",
            url=validated_url,
            wait_for_selector=wait_for_selector,
            actions=actions,
            timeout=remote_timeout,
        )
        duration = time.time() - start_time
        orig_len = raw_size_hint if raw_size_hint is not None else len(rendered_html)
        record_html_rendering_duration(duration)
        record_html_rendering_success(orig_len, len(rendered_html))
        return rendered_html

    async with _acquire_browser_slot(pdf_semaphore, fail_fast=True, url=validated_url):
        start_time = time.time()
        if actions:
            rendered_html = await render_html_with_playwright(
                validated_url, wait_for_selector, actions=actions
            )
        else:
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


async def _maybe_extract_ocr(
    content: bytes,
    metadata: ResponseMetadata,
    *,
    output_format: Literal["text", "markdown"],
    force_ocr: bool,
    ocr_language: str | None,
) -> tuple[str, dict[str, str]] | None:
    """Run OCR when response is an image (or validate when ?ocr=true is forced)."""
    content_type = metadata["content_type"]
    if is_ocr_image_content_type(content_type):
        extracted = await extract_text_from_image(content, language=ocr_language)
        body = (
            format_ocr_markdown(extracted, metadata["url"])
            if output_format == "markdown"
            else extracted
        )
        headers = {
            "X-Original-URL": metadata["url"],
            "X-Original-Content-Type": content_type,
            "X-Content-Length": str(metadata["size"]),
            "X-OCR-Applied": "true",
        }
        if ocr_language:
            headers["X-OCR-Language"] = ocr_language
        return body, headers

    if force_ocr:
        raise URLValidationError(
            f"OCR requires an image content type, but origin returned '{content_type}'."
        )
    return None


async def _handle_converted_text_response(
    output_format: Literal["text", "markdown"],
    validated_url: str,
    content: bytes | None,
    metadata: ResponseMetadata | None,
    force_render: bool = False,
    wait_for_selector: str | None = None,
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
    http_fallback: HTTPFallbackFetcher | None = None,
    already_rendered: bool = False,
    actions: Sequence[BrowserAction] | None = None,
    force_ocr: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Shared pipeline for text/plain and text/markdown responses (HTML, Playwright, or OCR)."""
    media_type = (
        "text/markdown; charset=utf-8"
        if output_format == "markdown"
        else "text/plain; charset=utf-8"
    )
    convert_fn = (
        convert_content_to_markdown if output_format == "markdown" else convert_content_to_text
    )

    if already_rendered:
        assert content is not None
        orig_url = metadata["url"] if metadata is not None else validated_url
        orig_ct = metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
        orig_len = str(metadata["size"] if metadata is not None else len(content))
        converted = convert_fn(content, "text/html")
        return Response(
            content=converted,
            media_type=media_type,
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
                actions=actions,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            orig_url = metadata["url"] if metadata is not None else validated_url
            orig_ct = (
                metadata["content_type"] if metadata is not None else "text/html; charset=utf-8"
            )
            orig_len = str(metadata["size"] if metadata is not None else len(rendered_html))
            converted = convert_fn(rendered_html, "text/html")
            return Response(
                content=converted,
                media_type=media_type,
                headers={
                    "X-Original-URL": orig_url,
                    "X-Original-Content-Type": orig_ct,
                    "X-Content-Length": orig_len,
                    "X-Rendered-With-JS": "true",
                },
            )
        except (SelectorTimeoutError, BrowserActionError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Playwright {output_format} rendering failed for {validated_url}: {e}")
            record_html_rendering_failure()
            if content is None or metadata is None:
                if http_fallback is None:
                    raise
                content, metadata = await http_fallback()
            converted = convert_fn(content, metadata["content_type"])
            return Response(
                content=converted,
                media_type=media_type,
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

    ocr_result = await _maybe_extract_ocr(
        content,
        metadata,
        output_format=output_format,
        force_ocr=force_ocr,
        ocr_language=ocr_language,
    )
    if ocr_result is not None:
        ocr_body, ocr_headers = ocr_result
        return Response(content=ocr_body, media_type=media_type, headers=ocr_headers)

    converted = convert_fn(content, metadata["content_type"])

    if not force_render and "html" in metadata["content_type"].lower():
        logger.info(
            f"BS4 {output_format} extraction for {validated_url}: {len(converted)} characters extracted"
        )
        converted = await _playwright_fallback_for_content(
            validated_url,
            converted,
            content,
            metadata["content_type"],
            output_format,
            pdf_semaphore=pdf_semaphore,
        )

    headers = {
        "X-Original-URL": metadata["url"],
        "X-Original-Content-Type": metadata["content_type"],
        "X-Content-Length": str(metadata["size"]),
    }
    if force_render:
        headers["X-Rendered-With-JS"] = "false"

    return Response(content=converted, media_type=media_type, headers=headers)


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
    actions: Sequence[BrowserAction] | None = None,
    force_ocr: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Handle plain text format response (HTML extraction or image OCR)."""
    return await _handle_converted_text_response(
        "text",
        validated_url,
        content,
        metadata,
        force_render,
        wait_for_selector,
        pdf_semaphore=pdf_semaphore,
        http_fallback=http_fallback,
        already_rendered=already_rendered,
        actions=actions,
        force_ocr=force_ocr,
        ocr_language=ocr_language,
        worker_queue=worker_queue,
        remote_timeout=remote_timeout,
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
    actions: Sequence[BrowserAction] | None = None,
    force_ocr: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Handle markdown format response (HTML conversion or image OCR)."""
    return await _handle_converted_text_response(
        "markdown",
        validated_url,
        content,
        metadata,
        force_render,
        wait_for_selector,
        pdf_semaphore=pdf_semaphore,
        http_fallback=http_fallback,
        already_rendered=already_rendered,
        actions=actions,
        force_ocr=force_ocr,
        ocr_language=ocr_language,
        worker_queue=worker_queue,
        remote_timeout=remote_timeout,
    )


async def handle_pdf_response(
    validated_url: str,
    metadata: ResponseMetadata | None,
    pdf_semaphore: asyncio.Semaphore,
    *,
    actions: Sequence[BrowserAction] | None = None,
    wait_for_selector: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Handle PDF format response with concurrency control."""
    logger.info(f"Generating PDF for: {validated_url}")

    if worker_queue is not None:
        pdf_content = await run_remote_browser_task(
            worker_queue,
            kind="pdf",
            url=validated_url,
            wait_for_selector=wait_for_selector,
            actions=actions,
            timeout=remote_timeout,
        )
    else:
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
            if actions or wait_for_selector:
                pdf_content = await generate_pdf_from_url(
                    validated_url,
                    actions=actions,
                    wait_for_selector=wait_for_selector,
                )
            else:
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
    actions: Sequence[BrowserAction] | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Handle HTML format response with optional Playwright rendering."""
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
                actions=actions,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
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
        except (SelectorTimeoutError, BrowserActionError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Playwright HTML rendering failed for {validated_url}: {e}")
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
                            f"❌ Playwright HTML rendering failed for {validated_url}: {e}"
                        )
                        logger.info(f"Falling back to raw HTML for {validated_url}")
                        record_html_rendering_failure()
                        html_content = content
                else:
                    html_content = content
        else:
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

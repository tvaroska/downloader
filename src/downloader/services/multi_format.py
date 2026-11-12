"""Multi-format parallel content conversion extracted from content_processor."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Response

from ..browser.actions import BrowserAction, BrowserActionError
from ..content_converter import (
    SelectorTimeoutError,
    convert_content_to_markdown,
    convert_content_to_text,
)
from ..metrics import record_html_rendering_failure
from ..models.responses import ResponseMetadata
from .content_negotiation import _format_to_mime_type

if TYPE_CHECKING:
    from ..worker_queue import WorkerQueue
    from .content_processor import HTTPFallbackFetcher

logger = logging.getLogger(__name__)


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
    actions: Sequence[BrowserAction] | None = None,
    full_page: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> tuple[str, str]:
    """Process a single format for multi-format response."""
    from . import content_processor as cp

    try:
        if format_type == "json":
            if content is None or metadata is None:
                raise Exception("Raw content required for JSON format")
            response = await cp.handle_json_response(content, metadata)
            return ("application/json", bytes(response.body).decode("utf-8"))

        elif format_type == "text":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded text extraction")
                return ("text/plain", convert_content_to_text(content, metadata["content_type"]))
            response = await cp.handle_text_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
                actions=actions,
                ocr_language=ocr_language,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            return ("text/plain", bytes(response.body).decode("utf-8"))

        elif format_type == "markdown":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded markdown extraction")
                return (
                    "text/markdown",
                    convert_content_to_markdown(content, metadata["content_type"]),
                )
            response = await cp.handle_markdown_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
                actions=actions,
                ocr_language=ocr_language,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            return ("text/markdown", bytes(response.body).decode("utf-8"))

        elif format_type == "pdf":
            if worker_queue is None and pdf_semaphore.locked():
                raise Exception("PDF service at capacity")
            response = await cp.handle_pdf_response(
                url,
                metadata,
                pdf_semaphore,
                actions=actions,
                wait_for_selector=wait_for_selector,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            return ("application/pdf", base64.b64encode(bytes(response.body)).decode("utf-8"))

        elif format_type in {"screenshot", "screenshot_jpeg"}:
            if worker_queue is None and pdf_semaphore.locked():
                raise Exception("Screenshot service at capacity")
            shot_fmt: Literal["png", "jpeg"] = "jpeg" if format_type == "screenshot_jpeg" else "png"
            response = await cp.handle_screenshot_response(
                url,
                metadata,
                pdf_semaphore,
                screenshot_format=shot_fmt,
                full_page=full_page,
                wait_for_selector=wait_for_selector,
                actions=actions,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            mime = "image/jpeg" if shot_fmt == "jpeg" else "image/png"
            return (mime, base64.b64encode(bytes(response.body)).decode("utf-8"))

        elif format_type == "html":
            if degraded_force_render:
                if content is None or metadata is None:
                    raise Exception("Raw content required for degraded HTML response")
                return ("text/html", content.decode("utf-8", errors="ignore"))
            response = await cp.handle_html_response(
                url,
                content,
                metadata,
                force_render,
                wait_for_selector,
                pdf_semaphore=pdf_semaphore,
                already_rendered=already_rendered,
                actions=actions,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
            )
            return ("text/html", bytes(response.body).decode("utf-8", errors="ignore"))

        else:
            raise Exception(f"Unsupported format: {format_type}")

    except Exception as e:
        raise Exception(f"{format_type} processing failed: {e}")


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
    actions: Sequence[BrowserAction] | None = None,
    full_page: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> dict[str, Any]:
    """Process content into multiple formats in parallel."""
    from . import content_processor as cp

    rendered_dom: bytes | None = None
    rendered_meta: ResponseMetadata | None = None
    degraded_force_render = False

    can_render = content is None or (
        metadata is not None and "html" in metadata["content_type"].lower()
    )
    if force_render and can_render and any(fmt in {"html", "text", "markdown"} for fmt in formats):
        try:
            rendered_dom = await cp._render_html_once_with_semaphore(
                url,
                wait_for_selector,
                pdf_semaphore,
                len(content) if content is not None else None,
                actions=actions,
                worker_queue=worker_queue,
                remote_timeout=remote_timeout,
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
        except (SelectorTimeoutError, BrowserActionError, HTTPException):
            raise
        except Exception as e:
            logger.error(f"❌ Multi-format Playwright rendering failed for {url}: {e}")
            record_html_rendering_failure()
            degraded_force_render = True
            if (content is None or metadata is None) and http_fallback is not None:
                content, metadata = await http_fallback()

    browser_only = {"pdf", "screenshot", "screenshot_jpeg"}
    needs_http_fetch = any(fmt == "json" for fmt in formats) or (
        not force_render and any(fmt not in browser_only for fmt in formats)
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
                    actions=actions,
                    full_page=full_page,
                    ocr_language=ocr_language,
                    worker_queue=worker_queue,
                    remote_timeout=remote_timeout,
                )
            )
        tasks.append(task)
        format_types.append(format_type)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    response_dict: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for format_type, result in zip(format_types, results, strict=False):
        if isinstance(result, Exception):
            errors[_format_to_mime_type(format_type)] = str(result)
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
    actions: Sequence[BrowserAction] | None = None,
    full_page: bool = False,
    ocr_language: str | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
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
        actions=actions,
        full_page=full_page,
        ocr_language=ocr_language,
        worker_queue=worker_queue,
        remote_timeout=remote_timeout,
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

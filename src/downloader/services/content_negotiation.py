"""Accept header negotiation and binary/JSON/screenshot response builders."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from fastapi import HTTPException, Response

from ..browser.actions import BrowserAction
from ..models.responses import ErrorResponse, ResponseMetadata
from ..pdf_generator import PDFGeneratorError, generate_screenshot_from_url

if TYPE_CHECKING:
    from ..worker_queue import WorkerQueue

logger = logging.getLogger(__name__)


def _format_to_mime_type(format_type: str) -> str:
    """Convert internal format string to MIME type."""
    mapping = {
        "text": "text/plain",
        "html": "text/html",
        "markdown": "text/markdown",
        "pdf": "application/pdf",
        "screenshot": "image/png",
        "screenshot_jpeg": "image/jpeg",
        "json": "application/json",
    }
    return mapping.get(format_type, "application/octet-stream")


def parse_accept_header(accept_header: str | None) -> str:
    """Parse Accept header to determine single response format."""
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
    elif "image/png" in accept_header:
        return "screenshot"
    elif "image/jpeg" in accept_header or "image/jpg" in accept_header:
        return "screenshot_jpeg"
    elif "application/json" in accept_header:
        return "json"
    else:
        return "raw"


def parse_accept_headers(accept_headers: str | list[str] | None) -> list[str]:
    """Parse Accept header(s) to determine all requested response formats."""
    if not accept_headers:
        return []

    header_list = [accept_headers] if isinstance(accept_headers, str) else accept_headers

    media_types = []
    for header in header_list:
        for part in header.split(","):
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
        elif media_type == "image/png":
            formats.append("screenshot")
        elif media_type in ("image/jpeg", "image/jpg"):
            formats.append("screenshot_jpeg")
        elif media_type == "application/json":
            formats.append("json")

    seen: set[str] = set()
    unique_formats = []
    for fmt in formats:
        if fmt not in seen:
            seen.add(fmt)
            unique_formats.append(fmt)

    return unique_formats


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


async def run_remote_browser_task(
    worker_queue: WorkerQueue,
    *,
    kind: Literal["render_html", "pdf", "screenshot"],
    url: str,
    wait_for_selector: str | None = None,
    actions: Sequence[BrowserAction] | None = None,
    full_page: bool = False,
    screenshot_format: Literal["png", "jpeg"] = "png",
    timeout: float = 45.0,
) -> bytes:
    """Delegate a browser rendering/PDF/screenshot task to a split worker over Redis."""
    from ..content_converter import SelectorTimeoutError
    from ..worker_queue import BrowserTaskRequest

    req = BrowserTaskRequest(
        kind=kind,
        url=url,
        wait_for_selector=wait_for_selector,
        actions=[a.model_dump() for a in (actions or [])],
        full_page=full_page,
        screenshot_format=screenshot_format,
    )
    resp = await worker_queue.enqueue_browser_task(req, timeout=timeout)
    if not resp.success:
        if resp.error_type == "selector_timeout_error":
            raise SelectorTimeoutError(wait_for_selector or "", int(timeout * 1000))
        if resp.error_type == "browser_action_error":
            from ..browser.actions import BrowserActionError

            raise BrowserActionError(resp.error or "Remote browser action failed")
        raise PDFGeneratorError(
            resp.error or f"Remote worker browser task ({kind}) failed",
            status_code=resp.status_code,
        )
    return resp.decode_bytes()


async def handle_screenshot_response(
    validated_url: str,
    metadata: ResponseMetadata | None,
    pdf_semaphore: asyncio.Semaphore,
    *,
    screenshot_format: Literal["png", "jpeg"] = "png",
    full_page: bool = False,
    wait_for_selector: str | None = None,
    actions: Sequence[BrowserAction] | None = None,
    worker_queue: WorkerQueue | None = None,
    remote_timeout: float = 45.0,
) -> Response:
    """Handle PNG or JPEG screenshot capture with concurrency control."""
    logger.info(
        f"Capturing {screenshot_format} screenshot for: {validated_url} (full_page={full_page})"
    )

    if worker_queue is not None:
        img_bytes = await run_remote_browser_task(
            worker_queue,
            kind="screenshot",
            url=validated_url,
            wait_for_selector=wait_for_selector,
            actions=actions,
            full_page=full_page,
            screenshot_format=screenshot_format,
            timeout=remote_timeout,
        )
    else:
        if pdf_semaphore.locked():
            logger.warning(
                f"Screenshot service at capacity, rejecting request for: {validated_url}"
            )
            raise HTTPException(
                status_code=503,
                detail=ErrorResponse(
                    error="Browser capture service temporarily unavailable. Please try again later.",
                    error_type="service_unavailable",
                ).model_dump(),
            )

        options: dict[str, Any] = {"type": screenshot_format, "full_page": full_page}
        async with pdf_semaphore:
            img_bytes = await generate_screenshot_from_url(
                validated_url,
                options,
                actions=actions,
                wait_for_selector=wait_for_selector,
            )

    media_type = "image/jpeg" if screenshot_format == "jpeg" else "image/png"
    ext = "jpg" if screenshot_format == "jpeg" else "png"
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={
            "X-Original-URL": metadata["url"] if metadata is not None else validated_url,
            "X-Original-Content-Type": (
                metadata["content_type"] if metadata is not None else "text/html"
            ),
            "Content-Length": str(len(img_bytes)),
            "Content-Disposition": f'inline; filename="screenshot.{ext}"',
        },
    )

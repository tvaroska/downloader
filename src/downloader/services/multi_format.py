"""Pure multi-format parallel content conversion built on top of `convert()`."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

from ..content_converter import run_in_html_executor, should_use_playwright_for_html
from ..content_encoding import resolve_charset_and_decode
from ..metrics import record_html_rendering_failure
from ..models.browser_actions import BrowserAction, BrowserActionError
from ..models.responses import ResponseMetadata
from ..ports import (
    BrowserCapacityError,
    BrowserPoolError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    PDFGeneratorError,
    RobotsDisallowedError,
    SelectorTimeoutError,
)
from ..validation import URLValidationError
from .content_negotiation import format_to_mime_type
from .content_processor import (
    ConversionDeps,
    ConversionRequest,
    ConversionResult,
    OutputFormat,
    convert,
    render_html_with_metrics,
)

logger = logging.getLogger(__name__)

_HTML_DERIVED_FORMATS = frozenset({"html", "text", "markdown"})
_BINARY_FORMATS = frozenset({"pdf", "screenshot", "screenshot_jpeg"})


@dataclass(frozen=True)
class MultiFormatRequest:
    """Input contract for parallel multi-format `Accept` negotiation."""

    url: str
    formats: list[str]
    force_render: bool = False
    wait_for_selector: str | None = None
    actions: Sequence[BrowserAction] | None = None
    full_page: bool = False
    screenshot_format: Literal["png", "jpeg"] = "png"
    force_ocr: bool = False
    ocr_language: str | None = None
    content: bytes | None = None
    metadata: ResponseMetadata | None = None


def _encode_result_for_json_map(format_type: str, result: ConversionResult) -> tuple[str, str]:
    mime = format_to_mime_type(format_type)
    if format_type in _BINARY_FORMATS:
        return mime, base64.b64encode(result.body).decode("utf-8")
    if format_type == "html":
        decoded, _ = resolve_charset_and_decode(result.body, result.media_type)
        return mime, decoded
    return mime, result.body.decode("utf-8")


@dataclass(slots=True, frozen=True)
class _PreparedPayload:
    content: bytes | None
    metadata: ResponseMetadata | None
    rendered_dom: bytes | None
    rendered_meta: ResponseMetadata | None
    degraded_force_render: bool


async def _convert_one_format(
    format_type: str,
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
    prepared: _PreparedPayload,
) -> tuple[str, str]:
    if format_type not in {
        "text",
        "markdown",
        "html",
        "pdf",
        "screenshot",
        "screenshot_jpeg",
        "json",
    }:
        raise ValueError(f"Unsupported format: {format_type}")

    use_rendered = format_type in _HTML_DERIVED_FORMATS and prepared.rendered_dom is not None
    use_degraded = (
        format_type in _HTML_DERIVED_FORMATS and not use_rendered and prepared.degraded_force_render
    )
    skip_browser = use_rendered or use_degraded

    single_req = ConversionRequest(
        url=multi_req.url,
        output_format=cast(OutputFormat, format_type),
        force_render=multi_req.force_render
        and not skip_browser
        and format_type in _HTML_DERIVED_FORMATS,
        wait_for_selector=None if skip_browser else multi_req.wait_for_selector,
        actions=None if skip_browser else multi_req.actions,
        full_page=multi_req.full_page,
        screenshot_format=multi_req.screenshot_format,
        force_ocr=multi_req.force_ocr,
        ocr_language=multi_req.ocr_language,
        content=prepared.rendered_dom if use_rendered else prepared.content,
        metadata=prepared.rendered_meta if use_rendered else prepared.metadata,
        already_rendered=use_rendered,
        degraded_force_render=use_degraded,
    )
    res = await convert(single_req, deps)
    if format_type == "html":
        return await run_in_html_executor(_encode_result_for_json_map, format_type, res)
    return _encode_result_for_json_map(format_type, res)


async def _force_render_shared_dom(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
    content: bytes | None,
    metadata: ResponseMetadata | None,
) -> tuple[bytes | None, ResponseMetadata | None, bytes | None, ResponseMetadata | None, bool]:
    try:
        rendered_dom = await render_html_with_metrics(
            deps.renderer,
            multi_req.url,
            wait_for_selector=multi_req.wait_for_selector,
            actions=multi_req.actions,
            raw_size_hint=len(content) if content is not None else None,
            fail_fast=True,
        )
        rendered_meta: ResponseMetadata | None = None
        if rendered_dom is not None:
            rendered_meta = metadata or {
                "status_code": 200,
                "headers": {"content-type": "text/html; charset=utf-8"},
                "url": multi_req.url,
                "size": len(rendered_dom),
                "content_type": "text/html; charset=utf-8",
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            }
        return content, metadata, rendered_dom, rendered_meta, False
    except (
        SelectorTimeoutError,
        BrowserActionError,
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        BrowserCapacityError,
        ContentTooLargeError,
    ):
        raise
    except (PDFGeneratorError, BrowserPoolError, RuntimeError, OSError, TimeoutError) as exc:
        logger.error(f"Multi-format Playwright rendering failed for {multi_req.url}: {exc}")
        record_html_rendering_failure()
        if (content is None or metadata is None) and deps.http_fetcher is not None:
            content, metadata = await deps.http_fetcher()
        return content, metadata, None, None, True


async def _auto_render_shared_dom_if_needed(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
    content: bytes | None,
    metadata: ResponseMetadata | None,
) -> tuple[bytes | None, ResponseMetadata | None, bool]:
    html_format_count = sum(1 for fmt in multi_req.formats if fmt in _HTML_DERIVED_FORMATS)
    if (
        multi_req.force_ocr
        or html_format_count <= 1
        or content is None
        or metadata is None
        or "html" not in metadata["content_type"].lower()
    ):
        return None, None, False
    if not await run_in_html_executor(
        should_use_playwright_for_html, multi_req.url, content, metadata["content_type"]
    ):
        return None, None, True
    try:
        rendered_dom = await render_html_with_metrics(
            deps.renderer,
            multi_req.url,
            wait_for_selector=multi_req.wait_for_selector,
            raw_size_hint=len(content),
            fail_fast=False,
        )
        return rendered_dom, (metadata if rendered_dom is not None else None), True
    except (
        SelectorTimeoutError,
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        ContentTooLargeError,
    ):
        raise
    except (
        PDFGeneratorError,
        BrowserCapacityError,
        BrowserPoolError,
        RuntimeError,
        OSError,
        TimeoutError,
    ) as exc:
        logger.warning(f"Auto-detected multi-format HTML render failed for {multi_req.url}: {exc}")
        record_html_rendering_failure()
        return None, None, True


async def _prepare_shared_dom(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
) -> _PreparedPayload:
    """Fetch HTTP and/or render HTML at most once across all requested formats."""
    content = multi_req.content
    metadata = multi_req.metadata
    rendered_dom: bytes | None = None
    rendered_meta: ResponseMetadata | None = None
    degraded_force_render = False

    has_html_formats = any(fmt in _HTML_DERIVED_FORMATS for fmt in multi_req.formats)
    can_render = content is None or (
        metadata is not None and "html" in metadata["content_type"].lower()
    )

    if multi_req.force_render and not multi_req.force_ocr and can_render and has_html_formats:
        (
            content,
            metadata,
            rendered_dom,
            rendered_meta,
            degraded_force_render,
        ) = await _force_render_shared_dom(multi_req, deps, content, metadata)

    needs_http = any(fmt == "json" for fmt in multi_req.formats) or (
        (not multi_req.force_render or multi_req.force_ocr)
        and any(fmt not in _BINARY_FORMATS for fmt in multi_req.formats)
    )
    if needs_http and (content is None or metadata is None) and deps.http_fetcher is not None:
        content, metadata = await deps.http_fetcher()

    if not multi_req.force_render and rendered_dom is None:
        (
            rendered_dom,
            rendered_meta,
            skip_per_format_render,
        ) = await _auto_render_shared_dom_if_needed(multi_req, deps, content, metadata)
        if rendered_dom is None and skip_per_format_render:
            degraded_force_render = True

    return _PreparedPayload(
        content=content,
        metadata=metadata,
        rendered_dom=rendered_dom,
        rendered_meta=rendered_meta,
        degraded_force_render=degraded_force_render,
    )


@asynccontextmanager
async def _maybe_hold_browser_slot(renderer: Any, url: str) -> AsyncIterator[None]:
    holder = getattr(renderer, "hold_request_slot", None)
    if callable(holder):
        async with holder(url):
            yield
    else:
        yield


async def _run_sequential_browser_formats(
    indexed_formats: list[tuple[int, str]],
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
    prepared: _PreparedPayload,
    results: list[tuple[str, str] | BaseException | None],
) -> None:
    if not indexed_formats:
        return

    async def _run_locked() -> None:
        async with _maybe_hold_browser_slot(deps.renderer, multi_req.url):
            for idx, fmt in indexed_formats:
                (outcome,) = await asyncio.gather(
                    _convert_one_format(fmt, multi_req, deps, prepared),
                    return_exceptions=True,
                )
                results[idx] = outcome

    (slot_outcome,) = await asyncio.gather(_run_locked(), return_exceptions=True)
    if isinstance(slot_outcome, BaseException):
        for idx, _fmt in indexed_formats:
            if results[idx] is None:
                results[idx] = slot_outcome


def _collect_multi_format_response(
    formats: list[str],
    results: list[tuple[str, str] | BaseException | None],
) -> dict[str, Any]:
    response_dict: dict[str, Any] = {}
    errors: dict[str, str] = {}
    first_error: Exception | None = None
    for fmt, item in zip(formats, results, strict=True):
        if isinstance(item, Exception):
            if first_error is None:
                first_error = item
            errors[format_to_mime_type(fmt)] = f"{fmt} processing failed: {item}"
        elif isinstance(item, BaseException):
            raise item
        elif item is not None:
            mime_type, content_str = item
            response_dict[mime_type] = content_str

    if not response_dict and first_error is not None:
        raise first_error
    if errors:
        response_dict["errors"] = errors
    return response_dict


async def _run_multiple_formats(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
) -> tuple[dict[str, Any], bool | None]:
    """Convert a single URL into multiple requested formats, serializing browser captures per request."""
    prepared = await _prepare_shared_dom(multi_req, deps)
    results: list[tuple[str, str] | BaseException | None] = [None] * len(multi_req.formats)
    parallel_tasks: list[tuple[int, asyncio.Task[tuple[str, str]]]] = []
    browser_formats: list[tuple[int, str]] = []

    for idx, fmt in enumerate(multi_req.formats):
        if fmt in _BINARY_FORMATS:
            browser_formats.append((idx, fmt))
        else:
            parallel_tasks.append(
                (idx, asyncio.create_task(_convert_one_format(fmt, multi_req, deps, prepared)))
            )

    browser_runner = asyncio.create_task(
        _run_sequential_browser_formats(browser_formats, multi_req, deps, prepared, results)
    )
    all_tasks: list[asyncio.Task[Any]] = [browser_runner, *(t for _, t in parallel_tasks)]
    try:
        if parallel_tasks:
            parallel_outcomes = await asyncio.gather(
                *(task for _, task in parallel_tasks),
                return_exceptions=True,
            )
            for (idx, _), outcome in zip(parallel_tasks, parallel_outcomes, strict=True):
                results[idx] = outcome
        await browser_runner
    finally:
        pending = [t for t in all_tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    response_dict = _collect_multi_format_response(multi_req.formats, results)
    has_html_formats = any(fmt in _HTML_DERIVED_FORMATS for fmt in multi_req.formats)
    rendered_with_js: bool | None = (
        (prepared.rendered_dom is not None) if has_html_formats else None
    )
    return response_dict, rendered_with_js


async def process_multiple_formats(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
) -> dict[str, Any]:
    """Convert a single URL into multiple requested formats in parallel, rendering DOM at most once."""
    response_dict, _ = await _run_multiple_formats(multi_req, deps)
    return response_dict


async def convert_multiple_formats(
    multi_req: MultiFormatRequest,
    deps: ConversionDeps,
) -> ConversionResult:
    """Return a serialized `application/json` `ConversionResult` for multi-format `Accept` requests."""
    results_dict, rendered_with_js = await _run_multiple_formats(multi_req, deps)
    orig_url = multi_req.metadata["url"] if multi_req.metadata is not None else multi_req.url
    body = json.dumps(results_dict).encode("utf-8")
    return ConversionResult(
        body=body,
        media_type="application/json",
        headers={
            "X-Original-URL": orig_url,
            "X-Multi-Format": "true",
            "X-Requested-Formats": ",".join(multi_req.formats),
        },
        original_content_type="application/json",
        rendered_with_js=rendered_with_js,
    )

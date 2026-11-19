"""Pure single-entrypoint `convert(request, deps) -> ConversionResult` format pipeline."""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from ..content_converter import (
    convert_content,
    run_in_html_executor,
    should_use_playwright_for_html,
)
from ..content_encoding import is_text_convertible_content_type, resolve_charset_and_decode
from ..metrics import (
    record_html_rendering_detection,
    record_html_rendering_duration,
    record_html_rendering_failure,
    record_html_rendering_success,
)
from ..models.browser_actions import BrowserAction, BrowserActionError
from ..models.responses import ResponseMetadata
from ..ports import (
    BrowserCapacityError,
    BrowserPoolError,
    BrowserRenderer,
    ContentConversionError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    PDFGeneratorError,
    RobotsDisallowedError,
    SelectorTimeoutError,
    UnsupportedMediaTypeError,
)
from ..transformers.ocr import (
    extract_text_from_image,
    format_ocr_markdown,
    is_ocr_image_content_type,
)
from ..validation import URLValidationError
from .content_negotiation import format_to_mime_type, parse_accept_header, parse_accept_headers

logger = logging.getLogger(__name__)

HTTPFallbackFetcher = Callable[[], Awaitable[tuple[bytes, ResponseMetadata]]]
OutputFormat = Literal[
    "text",
    "markdown",
    "html",
    "pdf",
    "screenshot",
    "screenshot_jpeg",
    "json",
    "raw",
]

__all__ = [
    "ConversionDeps",
    "ConversionRequest",
    "ConversionResult",
    "HTTPFallbackFetcher",
    "OutputFormat",
    "convert",
    "format_to_mime_type",
    "parse_accept_header",
    "parse_accept_headers",
    "render_html_with_metrics",
]


@dataclass(frozen=True)
class ConversionRequest:
    """Pure input contract for single-URL format conversion across routes, batch, and schedules."""

    url: str
    output_format: OutputFormat = "text"
    force_render: bool = False
    wait_for_selector: str | None = None
    actions: Sequence[BrowserAction] | None = None
    full_page: bool = False
    screenshot_format: Literal["png", "jpeg"] = "png"
    force_ocr: bool = False
    ocr_language: str | None = None
    content: bytes | None = None
    metadata: ResponseMetadata | None = None
    already_rendered: bool = False
    degraded_force_render: bool = False


@dataclass(frozen=True)
class ConversionDeps:
    """Collaborator ports required by `convert()`."""

    renderer: BrowserRenderer
    http_fetcher: HTTPFallbackFetcher | None = None


@dataclass(frozen=True)
class ConversionResult:
    """Pure conversion output mapped to HTTP responses, batch items, or schedule records."""

    body: bytes
    media_type: str
    headers: dict[str, str]
    original_content_type: str
    rendered_with_js: bool | None = None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class _HeaderSpec:
    original_url: str
    original_content_type: str | None = None
    original_size: int | None = None
    content_length: int | None = None
    rendered_with_js: bool | None = None
    ocr_applied: bool = False
    ocr_language: str | None = None
    content_disposition: str | None = None


def _build_conversion_headers(spec: _HeaderSpec) -> dict[str, str]:
    """Construct canonical `X-Original-*` and content metadata headers in one place."""
    headers: dict[str, str] = {"X-Original-URL": spec.original_url}
    if spec.original_content_type is not None:
        headers["X-Original-Content-Type"] = spec.original_content_type
    if spec.original_size is not None:
        headers["X-Content-Length"] = str(spec.original_size)
    if spec.content_length is not None:
        headers["Content-Length"] = str(spec.content_length)
    if spec.rendered_with_js is not None:
        headers["X-Rendered-With-JS"] = "true" if spec.rendered_with_js else "false"
    if spec.ocr_applied:
        headers["X-OCR-Applied"] = "true"
        if spec.ocr_language:
            headers["X-OCR-Language"] = spec.ocr_language
    if spec.content_disposition is not None:
        headers["Content-Disposition"] = spec.content_disposition
    return headers


def _source_meta(
    req: ConversionRequest,
    fallback_size: int,
    *,
    default_ct: str = "text/html; charset=utf-8",
) -> tuple[str, str, int]:
    if req.metadata is not None:
        return req.metadata["url"], req.metadata["content_type"], req.metadata["size"]
    return req.url, default_ct, fallback_size


def _make_result(
    body: bytes,
    media_type: str,
    header_spec: _HeaderSpec,
    *,
    original_content_type: str,
) -> ConversionResult:
    return ConversionResult(
        body=body,
        media_type=media_type,
        headers=_build_conversion_headers(header_spec),
        original_content_type=original_content_type,
        rendered_with_js=header_spec.rendered_with_js,
    )


async def render_html_with_metrics(  # noqa: PLR0913
    renderer: BrowserRenderer,
    url: str,
    *,
    wait_for_selector: str | None = None,
    actions: Sequence[BrowserAction] | None = None,
    raw_size_hint: int | None = None,
    fail_fast: bool = True,
) -> bytes | None:
    """Execute HTML rendering through `BrowserRenderer` and record rendering metrics."""
    record_html_rendering_detection()
    started = time.perf_counter()
    rendered = await renderer.render_html(
        url,
        wait_for_selector=wait_for_selector,
        actions=actions,
        fail_fast=fail_fast,
    )
    if rendered is None:
        return None
    duration = time.perf_counter() - started
    orig_len = raw_size_hint if raw_size_hint is not None else len(rendered)
    record_html_rendering_duration(duration)
    record_html_rendering_success(orig_len, len(rendered))
    return rendered


async def _require_http_payload(
    req: ConversionRequest,
    deps: ConversionDeps,
) -> tuple[bytes, ResponseMetadata]:
    if req.content is not None and req.metadata is not None:
        return req.content, req.metadata
    if deps.http_fetcher is None:
        raise ValueError("content and metadata or http_fetcher must be provided")
    return await deps.http_fetcher()


async def _convert_pdf(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    pdf_bytes = await deps.renderer.render_pdf(
        req.url,
        wait_for_selector=req.wait_for_selector,
        actions=req.actions,
    )
    orig_url, orig_ct, _ = _source_meta(req, len(pdf_bytes), default_ct="text/html")
    return _make_result(
        pdf_bytes,
        "application/pdf",
        _HeaderSpec(
            original_url=orig_url,
            original_content_type=orig_ct,
            content_length=len(pdf_bytes),
            content_disposition='inline; filename="download.pdf"',
        ),
        original_content_type=orig_ct,
    )


async def _convert_screenshot(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    shot_fmt: Literal["png", "jpeg"] = (
        "jpeg" if req.output_format == "screenshot_jpeg" else req.screenshot_format
    )
    img_bytes = await deps.renderer.render_screenshot(
        req.url,
        screenshot_format=shot_fmt,
        full_page=req.full_page,
        wait_for_selector=req.wait_for_selector,
        actions=req.actions,
    )
    media_type = "image/jpeg" if shot_fmt == "jpeg" else "image/png"
    ext = "jpg" if shot_fmt == "jpeg" else "png"
    orig_url, orig_ct, _ = _source_meta(req, len(img_bytes), default_ct="text/html")
    return _make_result(
        img_bytes,
        media_type,
        _HeaderSpec(
            original_url=orig_url,
            original_content_type=orig_ct,
            content_length=len(img_bytes),
            content_disposition=f'inline; filename="screenshot.{ext}"',
        ),
        original_content_type=orig_ct,
    )


async def _convert_json(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    content, metadata = await _require_http_payload(req, deps)
    payload = json.dumps(
        {
            "success": True,
            "url": metadata["url"],
            "size": metadata["size"],
            "content_type": metadata["content_type"],
            "content": base64.b64encode(content).decode("utf-8"),
            "metadata": metadata,
        }
    ).encode("utf-8")
    return _make_result(
        payload,
        "application/json",
        _HeaderSpec(original_url=metadata["url"], original_size=metadata["size"]),
        original_content_type=metadata["content_type"],
    )


async def _convert_raw(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    content, metadata = await _require_http_payload(req, deps)
    orig_ct = metadata.get("content_type", "application/octet-stream")
    return _make_result(
        content,
        orig_ct,
        _HeaderSpec(original_url=metadata["url"], content_length=metadata["size"]),
        original_content_type=orig_ct,
    )


async def _maybe_convert_ocr(
    req: ConversionRequest,
    content: bytes,
    metadata: ResponseMetadata,
    fmt: Literal["text", "markdown"],
    media_type: str,
) -> ConversionResult | None:
    content_type = metadata["content_type"]
    if is_ocr_image_content_type(content_type):
        extracted = await extract_text_from_image(content, language=req.ocr_language)
        text_body = (
            format_ocr_markdown(extracted, metadata["url"]) if fmt == "markdown" else extracted
        )
        return _make_result(
            text_body.encode("utf-8"),
            media_type,
            _HeaderSpec(
                original_url=metadata["url"],
                original_content_type=content_type,
                original_size=metadata["size"],
                ocr_applied=True,
                ocr_language=req.ocr_language,
            ),
            original_content_type=content_type,
        )
    if req.force_ocr:
        raise URLValidationError(
            f"OCR requires an image content type, but origin returned '{content_type}'."
        )
    return None


def _text_result_from_meta(
    text: str,
    media_type: str,
    meta: tuple[str, str, int],
    *,
    rendered_with_js: bool | None,
) -> ConversionResult:
    orig_url, orig_ct, orig_size = meta
    return _make_result(
        text.encode("utf-8"),
        media_type,
        _HeaderSpec(
            original_url=orig_url,
            original_content_type=orig_ct,
            original_size=orig_size,
            rendered_with_js=rendered_with_js,
        ),
        original_content_type=orig_ct,
    )


async def _force_render_text_or_markdown(
    req: ConversionRequest,
    deps: ConversionDeps,
    fmt: Literal["text", "markdown"],
    media_type: str,
) -> ConversionResult:
    try:
        rendered_html = await render_html_with_metrics(
            deps.renderer,
            req.url,
            wait_for_selector=req.wait_for_selector,
            actions=req.actions,
            raw_size_hint=len(req.content) if req.content is not None else None,
            fail_fast=True,
        )
        assert rendered_html is not None
        meta = _source_meta(req, len(rendered_html))
        converted = await run_in_html_executor(
            convert_content, rendered_html, "text/html; charset=utf-8", fmt
        )
        return _text_result_from_meta(converted, media_type, meta, rendered_with_js=True)
    except (
        SelectorTimeoutError,
        BrowserActionError,
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        UnsupportedMediaTypeError,
        BrowserCapacityError,
        ContentTooLargeError,
        ContentConversionError,
    ):
        raise
    except (PDFGeneratorError, BrowserPoolError, RuntimeError, OSError, TimeoutError) as exc:
        logger.error(f"Playwright {fmt} rendering failed for {req.url}: {exc}")
        record_html_rendering_failure()
        if req.content is None and deps.http_fetcher is None:
            raise
        content, metadata = await _require_http_payload(req, deps)
        converted = await run_in_html_executor(
            convert_content, content, metadata["content_type"], fmt
        )
        meta = (metadata["url"], metadata["content_type"], metadata["size"])
        return _text_result_from_meta(converted, media_type, meta, rendered_with_js=False)


async def _try_auto_fallback_text_or_markdown(
    req: ConversionRequest,
    deps: ConversionDeps,
    content: bytes,
    content_type: str,
    fmt: Literal["text", "markdown"],
) -> str | None:
    if req.force_render or "html" not in content_type.lower():
        return None
    if not await run_in_html_executor(
        should_use_playwright_for_html, req.url, content, content_type
    ):
        return None
    try:
        return await deps.renderer.convert_fallback(req.url, fmt)
    except (
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        ContentTooLargeError,
        ContentConversionError,
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
        logger.warning(f"Playwright {fmt} auto-fallback failed for {req.url}: {exc}")
        return None


async def _convert_text_or_markdown(
    req: ConversionRequest,
    deps: ConversionDeps,
    fmt: Literal["text", "markdown"],
) -> ConversionResult:
    media_type = (
        "text/markdown; charset=utf-8" if fmt == "markdown" else "text/plain; charset=utf-8"
    )

    if req.already_rendered and not req.force_ocr:
        assert req.content is not None
        meta = _source_meta(req, len(req.content))
        converted = await run_in_html_executor(
            convert_content, req.content, "text/html; charset=utf-8", fmt
        )
        return _text_result_from_meta(converted, media_type, meta, rendered_with_js=True)

    if req.degraded_force_render and not req.force_ocr:
        content, metadata = await _require_http_payload(req, deps)
        converted = await run_in_html_executor(
            convert_content, content, metadata["content_type"], fmt
        )
        meta = (metadata["url"], metadata["content_type"], metadata["size"])
        return _text_result_from_meta(converted, media_type, meta, rendered_with_js=False)

    if req.force_render and (
        req.content is None
        or (req.metadata is not None and "html" in req.metadata["content_type"].lower())
    ):
        return await _force_render_text_or_markdown(req, deps, fmt, media_type)

    content, metadata = await _require_http_payload(req, deps)
    ocr_res = await _maybe_convert_ocr(req, content, metadata, fmt, media_type)
    if ocr_res is not None:
        return ocr_res

    is_html = "html" in metadata["content_type"].lower()
    fallback = await _try_auto_fallback_text_or_markdown(
        req, deps, content, metadata["content_type"], fmt
    )
    if fallback is not None:
        final_text = fallback
        rendered_flag: bool | None = True
    else:
        final_text = await run_in_html_executor(
            convert_content, content, metadata["content_type"], fmt
        )
        rendered_flag = False if (req.force_render or is_html) else None
    meta = (metadata["url"], metadata["content_type"], metadata["size"])
    return _text_result_from_meta(final_text, media_type, meta, rendered_with_js=rendered_flag)


def _static_html_result(
    content: bytes,
    metadata: ResponseMetadata,
    *,
    rendered_with_js: bool,
) -> ConversionResult:
    if not is_text_convertible_content_type(
        metadata["content_type"],
        allow_ocr_images=False,
        sample_bytes=content,
    ):
        raise UnsupportedMediaTypeError(metadata["content_type"], target_format="html")

    if "html" in metadata["content_type"].lower():
        body_bytes = content
        media_type = "text/html; charset=utf-8" if rendered_with_js else metadata["content_type"]
    else:
        decoded, _ = resolve_charset_and_decode(content, metadata["content_type"])
        body_bytes = decoded.encode("utf-8")
        media_type = "text/html; charset=utf-8"

    return _make_result(
        body_bytes,
        media_type,
        _HeaderSpec(
            original_url=metadata["url"],
            original_content_type=metadata["content_type"],
            original_size=metadata["size"],
            rendered_with_js=rendered_with_js,
        ),
        original_content_type=metadata["content_type"],
    )


def _rendered_html_result(body: bytes, meta: tuple[str, str, int]) -> ConversionResult:
    orig_url, orig_ct, orig_size = meta
    return _make_result(
        body,
        "text/html; charset=utf-8",
        _HeaderSpec(
            original_url=orig_url,
            original_content_type=orig_ct,
            original_size=orig_size,
            rendered_with_js=True,
        ),
        original_content_type=orig_ct,
    )


async def _force_render_html(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    try:
        rendered_html = await render_html_with_metrics(
            deps.renderer,
            req.url,
            wait_for_selector=req.wait_for_selector,
            actions=req.actions,
            raw_size_hint=len(req.content) if req.content is not None else None,
            fail_fast=True,
        )
        assert rendered_html is not None
        return _rendered_html_result(rendered_html, _source_meta(req, len(rendered_html)))
    except (
        SelectorTimeoutError,
        BrowserActionError,
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        UnsupportedMediaTypeError,
        BrowserCapacityError,
        ContentTooLargeError,
    ):
        raise
    except (PDFGeneratorError, BrowserPoolError, RuntimeError, OSError, TimeoutError) as exc:
        logger.error(f"Playwright HTML rendering failed for {req.url}: {exc}")
        record_html_rendering_failure()
        if req.content is None and deps.http_fetcher is None:
            raise
        content, metadata = await _require_http_payload(req, deps)
        return _static_html_result(content, metadata, rendered_with_js=False)


async def _convert_html(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    if req.already_rendered and not req.force_ocr:
        assert req.content is not None
        return _rendered_html_result(req.content, _source_meta(req, len(req.content)))

    if req.degraded_force_render and not req.force_ocr:
        content, metadata = await _require_http_payload(req, deps)
        return _static_html_result(content, metadata, rendered_with_js=False)

    if req.force_render and (
        req.content is None
        or (req.metadata is not None and "html" in req.metadata["content_type"].lower())
    ):
        return await _force_render_html(req, deps)

    content, metadata = await _require_http_payload(req, deps)
    if not is_text_convertible_content_type(
        metadata["content_type"],
        allow_ocr_images=False,
        sample_bytes=content,
    ):
        raise UnsupportedMediaTypeError(metadata["content_type"], target_format="html")

    if (
        "html" in metadata["content_type"].lower()
        and not req.force_render
        and await run_in_html_executor(
            should_use_playwright_for_html, req.url, content, metadata["content_type"]
        )
    ):
        try:
            rendered = await render_html_with_metrics(
                deps.renderer,
                req.url,
                wait_for_selector=req.wait_for_selector,
                raw_size_hint=len(content),
                fail_fast=False,
            )
            if rendered is not None:
                meta = (metadata["url"], metadata["content_type"], metadata["size"])
                return _rendered_html_result(rendered, meta)
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
            logger.warning(
                f"Auto-detected Playwright HTML rendering failed for {req.url}, serving static HTML: {exc}"
            )
            record_html_rendering_failure()

    return _static_html_result(content, metadata, rendered_with_js=False)


async def _convert_markdown(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    return await _convert_text_or_markdown(req, deps, "markdown")


async def _convert_text(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    return await _convert_text_or_markdown(req, deps, "text")


_FORMAT_HANDLERS: dict[
    str,
    Callable[[ConversionRequest, ConversionDeps], Awaitable[ConversionResult]],
] = {
    "pdf": _convert_pdf,
    "screenshot": _convert_screenshot,
    "screenshot_jpeg": _convert_screenshot,
    "json": _convert_json,
    "raw": _convert_raw,
    "html": _convert_html,
    "markdown": _convert_markdown,
    "text": _convert_text,
}


async def convert(req: ConversionRequest, deps: ConversionDeps) -> ConversionResult:
    """Single pure format-conversion core shared across routes, multi-format, batch, and scheduler."""
    handler = _FORMAT_HANDLERS.get(req.output_format)
    if handler is None:
        raise URLValidationError(f"Unsupported output format: {req.output_format}")
    return await handler(req, deps)

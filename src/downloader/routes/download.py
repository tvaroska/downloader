"""Single URL download endpoint mapping `convert()` and `classify_domain_error()` to HTTP."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response

from ..auth import get_api_key
from ..browser.actions import BrowserAction, parse_actions_param
from ..config import Settings
from ..dependencies import (
    BrowserRendererDep,
    DomainThrottleDep,
    HTTPClientDep,
    RobotsCheckerDep,
    SettingsDep,
)
from ..errors import classify_domain_error, log_classified_error
from ..http_client import HTTPClient
from ..models.responses import ErrorResponse, HTTPErrorResponse, ResponseMetadata
from ..services.content_processor import (
    ConversionDeps,
    ConversionRequest,
    ConversionResult,
    HTTPFallbackFetcher,
    OutputFormat,
    convert,
    parse_accept_header,
    parse_accept_headers,
)
from ..services.multi_format import MultiFormatRequest, convert_multiple_formats
from ..transformers.ocr import validate_ocr_language
from ..validation import URLValidationError, canonical_host, validate_url_async

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_DOWNLOAD_QUERY_PARAMS = frozenset(
    {
        "render",
        "wait_for",
        "output",
        "full_page",
        "screenshot_format",
        "actions",
        "ocr",
        "ocr_lang",
    }
)


@dataclass(frozen=True)
class _ParsedDownloadParams:
    validated_url: str
    formats: list[str]
    format_type: OutputFormat
    effective_render: bool
    wait_for: str | None
    actions: list[BrowserAction] | None
    full_page: bool
    screenshot_format: Literal["png", "jpeg"]
    force_ocr: bool
    ocr_language: str | None


def _extract_raw_target(request: Request, fallback_url: str) -> str:
    unexpected = [key for key in request.query_params if key not in _ALLOWED_DOWNLOAD_QUERY_PARAMS]
    if unexpected:
        bad_keys = ", ".join(sorted(set(unexpected)))
        raise URLValidationError(
            f"Unsupported query parameter(s): {bad_keys}. "
            "Percent-encode '?', '#', and '%' in the target URL (%3F, %23, %25)."
        )
    scope = getattr(request, "scope", None)
    raw_path = scope.get("raw_path") if isinstance(scope, dict) else None
    if isinstance(raw_path, bytes) and raw_path:
        return unquote(raw_path.decode("latin-1").lstrip("/"))
    return fallback_url


def _parse_query_options(
    actions_raw: str | None,
    ocr_lang_raw: str | None,
    default_ocr_lang: str,
) -> tuple[list[BrowserAction] | None, str | None]:
    """Translate client query-parameter syntax errors to `URLValidationError` (HTTP 400)."""
    try:
        parsed_actions = parse_actions_param(actions_raw)
        validated_lang = (
            validate_ocr_language(ocr_lang_raw, default_ocr_lang)
            if ocr_lang_raw is not None
            else None
        )
    except ValueError as exc:
        raise URLValidationError(str(exc)) from exc
    return (parsed_actions or None), validated_lang


def _resolve_single_format(
    accept: str | None,
    output: Literal["screenshot", "pdf"] | None,
    screenshot_format: Literal["png", "jpeg"],
    force_ocr: bool,
) -> tuple[OutputFormat, Literal["png", "jpeg"]]:
    if output == "pdf":
        return "pdf", screenshot_format
    if output == "screenshot":
        return "screenshot", screenshot_format
    negotiated = parse_accept_header(accept)
    if force_ocr and negotiated not in {"text", "markdown"}:
        negotiated = "text"
    shot_fmt: Literal["png", "jpeg"] = (
        "jpeg" if negotiated == "screenshot_jpeg" else screenshot_format
    )
    return cast(OutputFormat, negotiated), shot_fmt


def _build_http_fetcher(
    http_client: HTTPClient,
    validated_url: str,
    formats: list[str],
    format_type: OutputFormat,
) -> HTTPFallbackFetcher:
    require_text = False
    allow_ocr = False
    target_label: str = format_type
    if len(formats) > 1:
        http_formats = [f for f in formats if f not in {"pdf", "screenshot", "screenshot_jpeg"}]
        if http_formats and "json" not in http_formats:
            require_text = True
            allow_ocr = any(f in {"text", "markdown"} for f in http_formats)
            target_label = ",".join(http_formats)
    elif format_type in {"text", "markdown", "html"}:
        require_text = True
        allow_ocr = format_type in {"text", "markdown"}

    cached: tuple[bytes, ResponseMetadata] | None = None

    async def _fetch() -> tuple[bytes, ResponseMetadata]:
        nonlocal cached
        if cached is None:
            if require_text and isinstance(http_client, HTTPClient):
                cached = await http_client.download(
                    validated_url,
                    require_text_convertible=True,
                    allow_ocr_images=allow_ocr,
                    target_format=target_label,
                )
            else:
                cached = await http_client.download(validated_url)
        return cached

    return _fetch


async def _execute_download(
    params: _ParsedDownloadParams,
    output: Literal["screenshot", "pdf"] | None,
    deps: ConversionDeps,
) -> ConversionResult:
    if output is None and len(params.formats) > 1:
        multi_req = MultiFormatRequest(
            url=params.validated_url,
            formats=params.formats,
            force_render=params.effective_render,
            wait_for_selector=params.wait_for,
            actions=params.actions,
            full_page=params.full_page,
            screenshot_format=params.screenshot_format,
            force_ocr=params.force_ocr,
            ocr_language=params.ocr_language,
        )
        return await convert_multiple_formats(multi_req, deps)

    single_req = ConversionRequest(
        url=params.validated_url,
        output_format=params.format_type,
        force_render=params.effective_render,
        wait_for_selector=params.wait_for,
        actions=params.actions,
        full_page=params.full_page,
        screenshot_format=params.screenshot_format,
        force_ocr=params.force_ocr,
        ocr_language=params.ocr_language,
    )
    return await convert(single_req, deps)


@router.get(
    "/{url:path}",
    responses={
        400: {"model": HTTPErrorResponse, "description": "Invalid URL or request"},
        403: {"model": HTTPErrorResponse, "description": "Disallowed by origin robots.txt"},
        408: {"model": HTTPErrorResponse, "description": "Request timeout"},
        413: {"model": HTTPErrorResponse, "description": "Response content exceeds size limit"},
        415: {"model": HTTPErrorResponse, "description": "Unsupported binary content type"},
        422: {"model": HTTPErrorResponse, "description": "OCR image processing error"},
        429: {"model": HTTPErrorResponse, "description": "Rate limit or crawl-delay exceeded"},
        500: {"model": HTTPErrorResponse, "description": "Internal server error"},
        502: {"model": HTTPErrorResponse, "description": "Upstream server error"},
        503: {"model": HTTPErrorResponse, "description": "Service temporarily unavailable"},
        504: {"model": HTTPErrorResponse, "description": "Remote worker render timeout"},
    },
)
async def download_url(  # noqa: PLR0913
    request: Request,
    http_client: HTTPClientDep,
    renderer: BrowserRendererDep,
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
    output: Literal["screenshot", "pdf"] | None = Query(
        None,
        description="Explicit binary output format toggle (screenshot or pdf)",
    ),
    full_page: bool = Query(
        False,
        description="Capture full scrollable page when rendering a screenshot",
    ),
    screenshot_format: Literal["png", "jpeg"] = Query(
        "png",
        description="Image encoding when rendering a screenshot (png or jpeg)",
    ),
    actions: str | None = Query(
        None,
        max_length=8192,
        description="JSON array of declarative Playwright actions to execute prior to capture",
    ),
    ocr: bool = Query(
        False,
        description="Require OCR extraction (validates that origin returns an image)",
    ),
    ocr_lang: str | None = Query(
        None,
        max_length=64,
        description="Optional Tesseract OCR language code (e.g. 'eng', 'eng+deu')",
    ),
    _api_key: str | None = Depends(get_api_key),
) -> Response:
    """Download content from a single URL with content negotiation, OCR, and ethical crawling."""
    active_settings: Settings = getattr(request.app.state, "settings", None) or settings
    try:
        raw_target = _extract_raw_target(request, url)
        validated_url = await validate_url_async(raw_target, active_settings)
        parsed_actions, validated_ocr_lang = _parse_query_options(
            actions, ocr_lang, active_settings.ocr.default_language
        )
        logger.info(f"Processing download request for: {validated_url}")

        if active_settings.crawling.respect_robots_txt:
            crawl_delay = await robots_checker.check_allowed(
                validated_url,
                active_settings.crawling.user_agent,
            )
            await domain_throttle.wait_if_needed(canonical_host(validated_url), crawl_delay)

        formats = parse_accept_headers(request.headers.getlist("accept"))
        format_type, shot_fmt = _resolve_single_format(accept, output, screenshot_format, ocr)
        params = _ParsedDownloadParams(
            validated_url=validated_url,
            formats=formats,
            format_type=format_type,
            effective_render=render or (wait_for is not None) or bool(parsed_actions),
            wait_for=wait_for,
            actions=parsed_actions,
            full_page=full_page,
            screenshot_format=shot_fmt,
            force_ocr=ocr,
            ocr_language=validated_ocr_lang,
        )
        deps = ConversionDeps(
            renderer=renderer,
            http_fetcher=_build_http_fetcher(http_client, validated_url, formats, format_type),
        )
        result = await _execute_download(params, output, deps)
        return Response(
            content=result.body,
            media_type=result.media_type,
            headers=result.headers,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Map every domain and unexpected runtime error through the single classification table.
        classified = classify_domain_error(exc, context="download")
        log_classified_error(logger, url, exc, classified)
        raise HTTPException(
            status_code=classified.status_code,
            detail=ErrorResponse(
                error=classified.message,
                error_type=classified.error_type,
            ).model_dump(),
            headers=classified.headers,
        ) from exc

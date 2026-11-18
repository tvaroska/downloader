"""Single domain-exception classification table shared by routes, batch, and scheduler."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal

from .browser.actions import BrowserActionError
from .content_converter import SelectorTimeoutError
from .crawling import CrawlDelayExceededError, RobotsDisallowedError
from .http_client import (
    ContentConversionError,
    ContentTooLargeError,
    DownloadError,
    HTTPClientError,
    HTTPTimeoutError,
    UnsupportedMediaTypeError,
)
from .pdf_generator import PDFGeneratorError
from .ports import BrowserCapacityError
from .transformers.ocr import OCRError, OCRUnavailableError
from .validation import URLValidationError

ErrorContext = Literal["download", "render", "batch", "schedule"]
LogLevel = Literal["info", "warning", "error", "exception"]


@dataclass(frozen=True)
class ErrorClassification:
    """Normalized HTTP status, error code, retryability, and log severity for a domain error."""

    status_code: int
    error_type: str
    message: str
    headers: dict[str, str] | None = None
    retryable: bool = True
    log_level: LogLevel = "warning"


_SIMPLE_RULES: tuple[tuple[type[Exception], int, str, bool, LogLevel], ...] = (
    (RobotsDisallowedError, 403, "robots_disallowed", False, "info"),
    (URLValidationError, 400, "validation_error", False, "warning"),
    (BrowserActionError, 400, "browser_action_error", False, "warning"),
    (SelectorTimeoutError, 408, "selector_timeout_error", True, "warning"),
    (OCRUnavailableError, 503, "ocr_unavailable", True, "warning"),
    (OCRError, 422, "ocr_error", False, "warning"),
    (ContentTooLargeError, 413, "content_too_large", False, "warning"),
    (UnsupportedMediaTypeError, 415, "unsupported_media_type", False, "warning"),
    (HTTPTimeoutError, 408, "timeout_error", True, "error"),
    (ContentConversionError, 500, "conversion_error", False, "error"),
)


def _classify_http_or_pdf_error(
    exc: HTTPClientError | PDFGeneratorError,
    context: ErrorContext,
) -> ErrorClassification:
    if isinstance(exc, HTTPClientError):
        raw_code = exc.status_code
        if raw_code is None or raw_code >= 500:
            return ErrorClassification(
                status_code=502,
                error_type="http_error",
                message=str(exc),
                retryable=True,
                log_level="error",
            )
        is_4xx = 400 <= raw_code < 500
        return ErrorClassification(
            status_code=raw_code,
            error_type="http_error",
            message=str(exc),
            retryable=(raw_code == 429 or not is_4xx),
            log_level="warning" if is_4xx else "error",
        )

    if exc.status_code is not None and 400 <= exc.status_code < 500:
        return ErrorClassification(
            status_code=exc.status_code,
            error_type="http_error",
            message=str(exc),
            retryable=(exc.status_code == 429),
            log_level="warning",
        )

    if context == "render":
        return ErrorClassification(
            status_code=500,
            error_type="render_error",
            message=f"Browser render failed: {exc}",
            retryable=True,
            log_level="error",
        )
    return ErrorClassification(
        status_code=500,
        error_type="pdf_generation_error",
        message=f"PDF generation failed: {exc}",
        retryable=True,
        log_level="error",
    )


def _classify_special_error(
    exc: Exception,
    context: ErrorContext,
    batch_timeout: int | None,
) -> ErrorClassification | None:
    if isinstance(exc, CrawlDelayExceededError):
        retry_secs = max(1, math.ceil(exc.retry_after))
        return ErrorClassification(
            status_code=429,
            error_type="crawl_delay_exceeded",
            message=str(exc),
            headers={"Retry-After": str(retry_secs)},
            retryable=True,
            log_level="warning",
        )
    if isinstance(exc, BrowserCapacityError):
        return ErrorClassification(
            status_code=503,
            error_type="service_unavailable",
            message="Browser rendering service temporarily unavailable. Please try again later.",
            retryable=True,
            log_level="warning",
        )
    if isinstance(exc, TimeoutError):
        msg = (
            f"Request timeout after {batch_timeout} seconds"
            if context == "batch" and batch_timeout is not None
            else str(exc)
        )
        return ErrorClassification(
            status_code=408 if context == "batch" else 504,
            error_type="worker_timeout" if context == "render" else "timeout_error",
            message=msg,
            retryable=True,
            log_level="error",
        )
    return None


def classify_domain_error(
    exc: Exception,
    *,
    context: ErrorContext = "download",
    batch_timeout: int | None = None,
) -> ErrorClassification:
    """Classify any domain or unexpected exception into a deterministic HTTP/batch contract."""
    special = _classify_special_error(exc, context, batch_timeout)
    if special is not None:
        return special

    for cls, status_code, error_type, retryable, log_level in _SIMPLE_RULES:
        if isinstance(exc, cls):
            return ErrorClassification(
                status_code=status_code,
                error_type=error_type,
                message=str(exc),
                retryable=retryable,
                log_level=log_level,
            )

    if isinstance(exc, (HTTPClientError, PDFGeneratorError)):
        return _classify_http_or_pdf_error(exc, context)

    if isinstance(exc, DownloadError):
        return ErrorClassification(
            status_code=500,
            error_type="download_error",
            message=str(exc),
            retryable=True,
            log_level="error",
        )

    return ErrorClassification(
        status_code=500,
        error_type="internal_error",
        message="Internal server error",
        retryable=True,
        log_level="exception",
    )


def log_classified_error(
    log: logging.Logger,
    target: str,
    exc: Exception,
    classified: ErrorClassification,
) -> None:
    """Log `exc` at the severity prescribed by `classified.log_level`."""
    msg = f"{classified.error_type} for {target}: {exc}"
    if classified.log_level == "info":
        log.info(msg)
    elif classified.log_level == "warning":
        log.warning(msg)
    elif classified.log_level == "error":
        log.error(msg)
    else:
        log.exception(msg)

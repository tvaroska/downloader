"""Typed exception hierarchy for the Downloader Python SDK."""

from __future__ import annotations


class DownloaderError(Exception):
    """Base error raised by the Downloader SDK."""


class APIError(DownloaderError):
    """Structured error returned by the Downloader HTTP API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.retry_after = retry_after


class ValidationError(APIError):
    """Raised on HTTP 400/422 request or URL validation failures."""


class RateLimitError(APIError):
    """Raised on HTTP 429 rate-limit exceeded responses."""


class ServiceUnavailableError(APIError):
    """Raised on HTTP 503 capacity, Redis unavailability, or connection-draining responses."""

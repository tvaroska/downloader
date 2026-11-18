"""Typed Python SDK for the REST API Downloader service."""

from .client import AsyncDownloaderClient, DownloaderClient
from .exceptions import (
    APIError,
    DownloaderError,
    RateLimitError,
    ServiceUnavailableError,
    ValidationError,
)
from .models import (
    BatchJobResults,
    BatchJobStatus,
    BatchJobSubmission,
    BatchURLItem,
    BrowserAction,
    DownloadResult,
    HealthStatus,
    ScheduleCreateRequest,
    ScheduleExecution,
    ScheduleHistory,
    ScheduleInfo,
)

__version__ = "0.7.0"

__all__ = [
    "APIError",
    "AsyncDownloaderClient",
    "BatchJobResults",
    "BatchJobStatus",
    "BatchJobSubmission",
    "BatchURLItem",
    "BrowserAction",
    "DownloadResult",
    "DownloaderClient",
    "DownloaderError",
    "HealthStatus",
    "RateLimitError",
    "ScheduleCreateRequest",
    "ScheduleExecution",
    "ScheduleHistory",
    "ScheduleInfo",
    "ServiceUnavailableError",
    "ValidationError",
    "__version__",
]

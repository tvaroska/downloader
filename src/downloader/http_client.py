"""Simplified HTTP client implementation using httpx with connection pooling."""

import logging
from enum import Enum
from typing import Any

import httpx

from .validation import sanitize_user_agent

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Base exception for download errors."""

    pass


class HTTPClientError(DownloadError):
    """HTTP client related errors."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class HTTPTimeoutError(DownloadError):
    """HTTP timeout errors."""

    pass


class RequestPriority(Enum):
    """Request priority levels (kept for API compatibility)."""

    HIGH = 1  # Online API requests
    LOW = 2  # Batch requests


class HTTPClient:
    """HTTP client with connection pooling."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str | None = None,
        max_concurrent: int = 20,
        max_concurrent_batch: int = 5,
    ):
        """
        Initialize HTTP client with connection pooling.

        Args:
            timeout: Request timeout in seconds
            max_redirects: Maximum number of redirects to follow
            user_agent: Custom user agent string
            max_concurrent: Max concurrent requests (kept for API compatibility)
            max_concurrent_batch: Max concurrent batch requests (kept for API compatibility)
        """
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = sanitize_user_agent(user_agent)

        # Connection limits for pooling
        limits = httpx.Limits(
            max_keepalive_connections=100,
            max_connections=200,
            keepalive_expiry=30.0,
        )

        # Create httpx client with connection pooling
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            limits=limits,
            http2=True,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
            },
        )

        logger.info(
            f"HTTP client initialized with connection pooling: "
            f"max_connections={limits.max_connections}, "
            f"max_keepalive={limits.max_keepalive_connections}"
        )

    async def download(
        self, url: str, priority: RequestPriority = RequestPriority.HIGH
    ) -> tuple[bytes, dict[str, Any]]:
        """
        Download content from a URL.

        Args:
            url: The URL to download from
            priority: Request priority (kept for API compatibility, not used)

        Returns:
            Tuple of (content_bytes, metadata_dict)

        Raises:
            HTTPClientError: For HTTP-related errors
            HTTPTimeoutError: For timeout errors
            DownloadError: For other download errors
        """
        return await self._do_download(url)

    async def _do_download(self, url: str) -> tuple[bytes, dict[str, Any]]:
        """Internal download implementation."""
        logger.info(f"Starting download from: {url}")

        try:
            response = await self._client.get(url)

            # Check for HTTP errors
            if response.status_code >= 400:
                raise HTTPClientError(
                    f"HTTP {response.status_code}: {response.reason_phrase}",
                    status_code=response.status_code,
                )

            content = response.content

            # Prepare metadata with connection info
            metadata = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "url": str(response.url),
                "size": len(content),
                "content_type": response.headers.get("content-type", "unknown"),
                "http_version": response.http_version,
                "connection_reused": getattr(response, "is_reused", None),
            }

            logger.info(
                f"Download completed: {len(content)} bytes, "
                f"status: {response.status_code}, "
                f"type: {metadata['content_type']}, "
                f"http_version: {response.http_version}"
            )

            return content, metadata

        except httpx.TimeoutException as e:
            logger.error(f"Timeout downloading {url}: {e}")
            raise HTTPTimeoutError(f"Request timed out after {self.timeout}s")

        except httpx.RequestError as e:
            logger.error(f"Request error downloading {url}: {e}")
            raise HTTPClientError(f"Request failed: {e}")

        except HTTPClientError:
            raise

        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            raise DownloadError(f"Download failed: {e}")

    def get_connection_stats(self) -> dict[str, Any]:
        """Get HTTP client connection statistics for monitoring."""
        try:
            return {
                "status": "healthy",
                "http2_enabled": True,
                "connection_limits": {
                    "max_connections": self._client.limits.max_connections,
                    "max_keepalive": self._client.limits.max_keepalive_connections,
                    "keepalive_expiry": self._client.limits.keepalive_expiry,
                },
            }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    async def close(self):
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()


# Global client instance for reuse
_global_client: HTTPClient | None = None


async def get_client() -> HTTPClient:
    """Get or create a global HTTP client instance."""
    global _global_client
    if _global_client is None:
        _global_client = HTTPClient()
    return _global_client


async def close_client():
    """Close the global HTTP client."""
    global _global_client
    if _global_client is not None:
        await _global_client.close()
        _global_client = None

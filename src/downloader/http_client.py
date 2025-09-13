"""Optimized HTTP client implementation using httpx with connection pooling."""

import logging
import time
from typing import Any

import httpx

from .validation import sanitize_user_agent

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Base exception for download errors."""

    pass


class HTTPClientError(DownloadError):
    """HTTP client related errors."""

    pass


class HTTPTimeoutError(DownloadError):
    """HTTP timeout errors."""

    pass


class CircuitBreakerOpen(DownloadError):
    """Circuit breaker is open, requests are being rejected."""

    pass


class CircuitBreaker:
    """Simple circuit breaker pattern for failing endpoints."""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 60.0):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Time to wait before attempting recovery
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"  # closed, open, half-open

    def call(self, func):
        """Decorator to wrap function calls with circuit breaker."""
        async def wrapper(*args, **kwargs):
            if self.state == "open":
                if time.time() - self.last_failure_time > self.recovery_timeout:
                    self.state = "half-open"
                    logger.info("Circuit breaker moving to half-open state")
                else:
                    raise CircuitBreakerOpen("Circuit breaker is open")

            try:
                result = await func(*args, **kwargs)
                if self.state == "half-open":
                    self.state = "closed"
                    self.failure_count = 0
                    logger.info("Circuit breaker recovered, moving to closed state")
                return result

            except Exception as e:
                self.failure_count += 1
                self.last_failure_time = time.time()

                if self.failure_count >= self.failure_threshold:
                    self.state = "open"
                    logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

                raise

        return wrapper


class HTTPClient:
    """Optimized HTTP client with connection pooling and circuit breaker."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str | None = None,
    ):
        """
        Initialize optimized HTTP client with connection pooling.

        Args:
            timeout: Request timeout in seconds
            max_redirects: Maximum number of redirects to follow
            user_agent: Custom user agent string
        """
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = sanitize_user_agent(user_agent)
        self.circuit_breaker = CircuitBreaker()

        # Optimized connection limits for high concurrency workloads
        limits = httpx.Limits(
            max_keepalive_connections=100,  # Keep-alive pool size
            max_connections=200,           # Total connection pool size
            keepalive_expiry=30.0,         # Keep connections alive for 30s
        )

        # Create httpx client with optimized configuration
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            limits=limits,
            http2=True,  # Enable HTTP/2 support for better multiplexing
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate, br",  # Add Brotli support
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",  # Ensure fresh content
            },
        )

        logger.info(f"HTTP client initialized with optimized connection pooling: "
                   f"max_connections={limits.max_connections}, "
                   f"max_keepalive={limits.max_keepalive_connections}, "
                   f"http2=True")

    async def download(self, url: str) -> tuple[bytes, dict[str, Any]]:
        """
        Download content from a URL with circuit breaker protection.

        Args:
            url: The URL to download from

        Returns:
            Tuple of (content_bytes, metadata_dict)

        Raises:
            HTTPClientError: For HTTP-related errors
            HTTPTimeoutError: For timeout errors
            CircuitBreakerOpen: When circuit breaker is open
            DownloadError: For other download errors
        """
        @self.circuit_breaker.call
        async def _do_download():
            logger.info(f"Starting download from: {url}")

            response = await self._client.get(url)

            # Check for HTTP errors
            if response.status_code >= 400:
                raise HTTPClientError(
                    f"HTTP {response.status_code}: {response.reason_phrase}"
                )

            content = response.content

            # Prepare metadata with connection info
            metadata = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "url": str(response.url),  # Final URL after redirects
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

        try:
            return await _do_download()

        except CircuitBreakerOpen:
            # Re-raise circuit breaker errors as is
            raise

        except httpx.TimeoutException as e:
            logger.error(f"Timeout downloading {url}: {e}")
            raise HTTPTimeoutError(f"Request timed out after {self.timeout}s")

        except httpx.RequestError as e:
            logger.error(f"Request error downloading {url}: {e}")
            raise HTTPClientError(f"Request failed: {e}")

        except HTTPClientError:
            # Re-raise HTTPClientError as is
            raise
        except Exception as e:
            logger.error(f"Unexpected error downloading {url}: {e}")
            raise DownloadError(f"Download failed: {e}")

    def get_connection_stats(self) -> dict[str, Any]:
        """Get HTTP client connection statistics for monitoring."""
        try:
            # Get connection pool stats if available
            pool = getattr(self._client, '_transport', None)
            if pool and hasattr(pool, '_pool'):
                connection_pool = pool._pool
                stats = {
                    "status": "healthy",
                    "circuit_breaker_state": self.circuit_breaker.state,
                    "circuit_breaker_failures": self.circuit_breaker.failure_count,
                    "http2_enabled": True,
                    "connection_limits": {
                        "max_connections": self._client.limits.max_connections,
                        "max_keepalive": self._client.limits.max_keepalive_connections,
                        "keepalive_expiry": self._client.limits.keepalive_expiry,
                    }
                }

                # Add detailed pool stats if available
                if hasattr(connection_pool, '_connections'):
                    stats.update({
                        "active_connections": len(getattr(connection_pool, '_connections', [])),
                        "pool_efficiency": "optimized" if stats["circuit_breaker_state"] == "closed" else "degraded"
                    })

                return stats
            else:
                return {
                    "status": "basic",
                    "circuit_breaker_state": self.circuit_breaker.state,
                    "http2_enabled": True
                }

        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "circuit_breaker_state": self.circuit_breaker.state
            }

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

"""HTTP client implementation using httpx."""

import logging
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


class HTTPClient:
    """HTTP client for downloading content from URLs."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str | None = None,
    ):
        """
        Initialize HTTP client.

        Args:
            timeout: Request timeout in seconds
            max_redirects: Maximum number of redirects to follow
            user_agent: Custom user agent string
        """
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = sanitize_user_agent(user_agent)

        # Create httpx client with configuration
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            },
        )

    async def download(self, url: str) -> tuple[bytes, dict[str, Any]]:
        """
        Download content from a URL.

        Args:
            url: The URL to download from

        Returns:
            Tuple of (content_bytes, metadata_dict)

        Raises:
            HTTPClientError: For HTTP-related errors
            HTTPTimeoutError: For timeout errors
            DownloadError: For other download errors
        """
        try:
            logger.info(f"Starting download from: {url}")

            response = await self._client.get(url)

            # Check for HTTP errors
            if response.status_code >= 400:
                raise HTTPClientError(
                    f"HTTP {response.status_code}: {response.reason_phrase}"
                )

            content = response.content

            # Prepare metadata
            metadata = {
                "status_code": response.status_code,
                "headers": dict(response.headers),
                "url": str(response.url),  # Final URL after redirects
                "size": len(content),
                "content_type": response.headers.get("content-type", "unknown"),
            }

            logger.info(
                f"Download completed: {len(content)} bytes, "
                f"status: {response.status_code}, "
                f"type: {metadata['content_type']}"
            )

            return content, metadata

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

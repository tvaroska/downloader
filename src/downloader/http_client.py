"""Optimized HTTP client implementation using httpx with connection pooling."""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlparse

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


class RequestPriority(Enum):
    """Request priority levels for queue management."""
    HIGH = 1  # Online API requests
    LOW = 2   # Batch requests


@dataclass
class QueuedRequest:
    """Represents a queued request with priority and metadata."""
    url: str
    priority: RequestPriority
    future: asyncio.Future
    timestamp: float
    retry_count: int = 0

    def __lt__(self, other):
        """For PriorityQueue comparison - lower priority value = higher priority."""
        if not isinstance(other, QueuedRequest):
            return NotImplemented
        # First compare by priority, then by timestamp for FIFO within priority
        if self.priority.value != other.priority.value:
            return self.priority.value < other.priority.value
        return self.timestamp < other.timestamp


class CircuitBreaker:
    """Per-domain circuit breaker pattern for failing endpoints."""

    def __init__(self, failure_threshold: int = 10, recovery_timeout: float = 60.0, half_open_max_calls: int = 3):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            recovery_timeout: Time to wait before attempting recovery
            half_open_max_calls: Max calls to test in half-open state
        """
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_max_calls = half_open_max_calls
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = 0.0
        self.state = "closed"  # closed, open, half-open
        self.half_open_calls = 0

    def can_execute(self) -> bool:
        """Check if request can be executed."""
        if self.state == "closed":
            return True
        elif self.state == "open":
            if time.time() - self.last_failure_time > self.recovery_timeout:
                self.state = "half-open"
                self.half_open_calls = 0
                logger.info(f"Circuit breaker moving to half-open state")
                return True
            return False
        elif self.state == "half-open":
            return self.half_open_calls < self.half_open_max_calls
        return False

    def record_success(self):
        """Record a successful request."""
        if self.state == "half-open":
            self.success_count += 1
            if self.success_count >= self.half_open_max_calls:
                self.state = "closed"
                self.failure_count = 0
                self.success_count = 0
                logger.info(f"Circuit breaker recovered, moving to closed state")
        else:
            self.failure_count = max(0, self.failure_count - 1)  # Gradual recovery

    def record_failure(self):
        """Record a failed request."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.state == "half-open":
            self.state = "open"
            logger.warning(f"Circuit breaker re-opened during half-open state")
        elif self.failure_count >= self.failure_threshold and self.state == "closed":
            self.state = "open"
            logger.warning(f"Circuit breaker opened after {self.failure_count} failures")

        if self.state == "half-open":
            self.half_open_calls += 1


class HTTPClient:
    """Optimized HTTP client with connection pooling and per-domain circuit breaker."""

    def __init__(
        self,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str | None = None,
        max_concurrent: int = 20,
        max_concurrent_batch: int = 5,
    ):
        """
        Initialize optimized HTTP client with priority queue and per-domain circuit breakers.

        Args:
            timeout: Request timeout in seconds
            max_redirects: Maximum number of redirects to follow
            user_agent: Custom user agent string
            max_concurrent: Max concurrent requests for API
            max_concurrent_batch: Max concurrent requests for batch processing
        """
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = sanitize_user_agent(user_agent)

        # Per-domain circuit breakers
        self.circuit_breakers: dict[str, CircuitBreaker] = {}

        # Request queue and semaphores
        self.request_queue = asyncio.PriorityQueue()
        self.api_semaphore = asyncio.Semaphore(max_concurrent)
        self.batch_semaphore = asyncio.Semaphore(max_concurrent_batch)

        # Queue processor task
        self._queue_processor_task = None
        self._shutdown_event = asyncio.Event()

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
                   f"http2=True, api_concurrent={max_concurrent}, "
                   f"batch_concurrent={max_concurrent_batch}")

        # Start queue processor
        self._start_queue_processor()

    def _start_queue_processor(self):
        """Start the background queue processor."""
        if self._queue_processor_task is None:
            self._queue_processor_task = asyncio.create_task(self._process_queue())

    async def _process_queue(self):
        """Process requests from the priority queue."""
        while not self._shutdown_event.is_set():
            try:
                # Wait for request with timeout to allow periodic shutdown checks
                priority_item = await asyncio.wait_for(
                    self.request_queue.get(), timeout=1.0
                )
                _, request = priority_item

                # Process request in background
                asyncio.create_task(self._execute_request(request))

            except asyncio.TimeoutError:
                # Normal timeout for shutdown checking
                continue
            except Exception as e:
                logger.error(f"Queue processing error: {e}")
                await asyncio.sleep(1.0)  # Brief pause on error

    async def _execute_request(self, request: QueuedRequest):
        """Execute a queued request with appropriate semaphore and circuit breaker."""
        domain = urlparse(request.url).netloc
        circuit_breaker = self._get_circuit_breaker(domain)

        # Choose semaphore based on priority
        semaphore = self.api_semaphore if request.priority == RequestPriority.HIGH else self.batch_semaphore

        try:
            # Check circuit breaker before acquiring semaphore
            if not circuit_breaker.can_execute():
                if request.priority == RequestPriority.LOW and request.retry_count < 3:
                    # Retry batch requests with exponential backoff
                    delay = min(30, 2 ** request.retry_count)
                    await asyncio.sleep(delay)
                    request.retry_count += 1
                    await self.request_queue.put((request.priority.value, request))
                    return
                else:
                    request.future.set_exception(CircuitBreakerOpen(f"Circuit breaker is open for {domain}"))
                    return

            async with semaphore:
                # Add small delay for batch requests to reduce load
                if request.priority == RequestPriority.LOW:
                    await asyncio.sleep(0.1)

                result = await self._do_download(request.url, circuit_breaker)
                circuit_breaker.record_success()
                request.future.set_result(result)

        except Exception as e:
            circuit_breaker.record_failure()

            # Retry logic for batch requests
            if (request.priority == RequestPriority.LOW and
                request.retry_count < 3 and
                not isinstance(e, CircuitBreakerOpen)):

                delay = min(10, 1 + request.retry_count * 2)
                await asyncio.sleep(delay)
                request.retry_count += 1
                await self.request_queue.put((request.priority.value, request))
            else:
                request.future.set_exception(e)

    def _get_circuit_breaker(self, domain: str) -> CircuitBreaker:
        """Get or create circuit breaker for domain."""
        if domain not in self.circuit_breakers:
            self.circuit_breakers[domain] = CircuitBreaker(
                failure_threshold=10,
                recovery_timeout=60.0,
                half_open_max_calls=3
            )
        return self.circuit_breakers[domain]

    async def download(self, url: str, priority: RequestPriority = RequestPriority.HIGH) -> tuple[bytes, dict[str, Any]]:
        """
        Download content from a URL with priority queue and per-domain circuit breaker.

        Args:
            url: The URL to download from
            priority: Request priority (HIGH for API, LOW for batch)

        Returns:
            Tuple of (content_bytes, metadata_dict)

        Raises:
            HTTPClientError: For HTTP-related errors
            HTTPTimeoutError: For timeout errors
            CircuitBreakerOpen: When circuit breaker is open
            DownloadError: For other download errors
        """
        # Create future for result
        future = asyncio.Future()
        request = QueuedRequest(
            url=url,
            priority=priority,
            future=future,
            timestamp=time.time()
        )

        # Add to queue
        await self.request_queue.put((priority.value, request))

        # Wait for result
        return await future

    async def _do_download(self, url: str, circuit_breaker: CircuitBreaker) -> tuple[bytes, dict[str, Any]]:
        """Internal download method with circuit breaker protection."""
        logger.info(f"Starting download from: {url}")

        try:
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
            circuit_breaker_stats = {
                domain: {
                    "state": cb.state,
                    "failures": cb.failure_count,
                    "last_failure": cb.last_failure_time
                }
                for domain, cb in self.circuit_breakers.items()
            }

            if pool and hasattr(pool, '_pool'):
                connection_pool = pool._pool
                stats = {
                    "status": "healthy",
                    "circuit_breakers": circuit_breaker_stats,
                    "queue_size": self.request_queue.qsize(),
                    "api_semaphore_available": self.api_semaphore._value,
                    "batch_semaphore_available": self.batch_semaphore._value,
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
                        "pool_efficiency": "optimized" if len([cb for cb in self.circuit_breakers.values() if cb.state != "closed"]) == 0 else "degraded"
                    })

                return stats
            else:
                return {
                    "status": "basic",
                    "circuit_breakers": circuit_breaker_stats,
                    "queue_size": self.request_queue.qsize(),
                    "http2_enabled": True
                }

        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "circuit_breakers": {}
            }

    async def close(self):
        """Close the HTTP client and release resources."""
        # Signal shutdown
        self._shutdown_event.set()

        # Wait for queue processor to finish
        if self._queue_processor_task:
            self._queue_processor_task.cancel()
            try:
                await self._queue_processor_task
            except asyncio.CancelledError:
                pass

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

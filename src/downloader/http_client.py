"""HTTP client implementation with connection pooling, redirect SSRF/robots checks, and byte caps."""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpcore
import httpx

from .content_encoding import (
    is_text_convertible_content_type,
    make_stateless_cookies,
    read_bounded_response_bytes,
)
from .models.responses import ResponseMetadata
from .ssrf_transport import SSRFSafeHTTPTransport, SSRFSafeNetworkBackend
from .validation import (
    EgressPolicy,
    URLValidationError,
    canonical_origin,
    redact_url_for_log,
    sanitize_custom_headers,
    sanitize_user_agent,
    validate_url_async,
)

logger = logging.getLogger(__name__)

RedirectPolicyHook = Callable[[str, str], Awaitable[None]]

_CUSTOM_HEADER_GUARD: contextvars.ContextVar[
    tuple[tuple[str, str, int] | None, frozenset[str]] | None
] = contextvars.ContextVar("http_client_custom_header_guard", default=None)


class DownloadError(Exception):
    """Base exception for download errors."""


class HTTPClientError(DownloadError):
    """HTTP client related errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HTTPTimeoutError(DownloadError):
    """HTTP timeout errors."""


class ContentTooLargeError(DownloadError):
    """Raised when response or rendered content exceeds max_download_size."""

    def __init__(
        self,
        max_size_or_msg: int | str = 0,
        actual_size: int | None = None,
        *,
        max_size: int | None = None,
    ) -> None:
        if isinstance(max_size_or_msg, str):
            resolved_max = max_size if max_size is not None else 0
            message = max_size_or_msg
        else:
            resolved_max = max_size if max_size is not None else int(max_size_or_msg)
            if actual_size is not None:
                message = (
                    f"Content size ({actual_size} bytes) exceeds maximum allowed size "
                    f"of {resolved_max} bytes"
                )
            else:
                message = f"Content size exceeds maximum allowed size of {resolved_max} bytes"
        super().__init__(message)
        self.max_size: int = resolved_max
        self.actual_size: int | None = actual_size


class UnsupportedMediaTypeError(DownloadError):
    """Raised when an upstream binary Content-Type cannot be converted to text/HTML/markdown."""

    def __init__(
        self,
        content_type: str,
        target_format: str = "text",
        message: str | None = None,
    ) -> None:
        self.content_type = content_type
        self.target_format = target_format
        resolved = message or (
            f"Unsupported binary content type '{content_type}' for {target_format} conversion"
        )
        super().__init__(resolved)


class ContentConversionError(DownloadError):
    """Raised when HTML-to-text/markdown extraction fails instead of silently returning raw HTML."""


def _iter_mapping_pairs(headers: Any) -> list[tuple[Any, Any]]:
    if isinstance(headers, (httpx.Headers, Mapping)):
        return list(headers.items())
    items_fn = getattr(headers, "items", None)
    if not callable(items_fn):
        return []
    try:
        return [item for item in items_fn() if isinstance(item, tuple) and len(item) == 2]
    except (TypeError, ValueError):
        return []


def _extract_header_case_insensitive(headers: Any, name: str) -> str | None:
    """Read a header value case-insensitively from httpx.Headers or a mapping."""
    target = name.lower()
    for key, val in _iter_mapping_pairs(headers):
        if isinstance(key, str) and key.lower() == target and isinstance(val, (str, int)):
            return str(val)
    getter = getattr(headers, "get", None)
    if callable(getter):
        val = getter(target)
        if val is None:
            val = getter(name)
        if isinstance(val, (str, int)):
            return str(val)
    return None


def _parse_content_length(headers: Any) -> int | None:
    """Parse a non-negative integer Content-Length header if present and valid."""
    raw = _extract_header_case_insensitive(headers, "content-length")
    if raw is None:
        return None
    raw_stripped = raw.strip()
    if not raw_stripped.isdigit():
        return None
    return int(raw_stripped)


class HTTPClient:
    """HTTP client with connection pooling, redirect SSRF checks, and streaming byte cap."""

    def __init__(  # noqa: PLR0913
        self,
        timeout: float = 30.0,
        max_redirects: int = 10,
        user_agent: str | None = None,
        max_download_size: int = 50 * 1024 * 1024,
        max_connections: int = 200,
        max_keepalive_connections: int = 100,
        keepalive_expiry: float = 30.0,
        http2: bool = True,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.user_agent = sanitize_user_agent(user_agent)
        self.max_download_size = max_download_size
        self.http2 = http2
        self._egress_policy = egress_policy
        self._redirect_policy = redirect_policy

        self._limits = httpx.Limits(
            max_keepalive_connections=max_keepalive_connections,
            max_connections=max_connections,
            keepalive_expiry=keepalive_expiry,
        )

        backend: httpcore.AsyncNetworkBackend | None = None
        if network_backend is not None:
            backend = (
                network_backend
                if isinstance(network_backend, SSRFSafeNetworkBackend)
                else SSRFSafeNetworkBackend(inner=network_backend, egress_policy=egress_policy)
            )

        active_transport = transport or SSRFSafeHTTPTransport(
            limits=self._limits,
            http2=http2,
            trust_env=False,
            backend=backend,
            egress_policy=egress_policy,
        )

        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=max_redirects,
            transport=active_transport,
            trust_env=False,
            cookies=make_stateless_cookies(),
            event_hooks={
                "request": [self._on_request_hook],
                "response": [self._on_response_hook],
            },
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
            f"max_connections={self._limits.max_connections}, "
            f"max_keepalive={self._limits.max_keepalive_connections}, "
            f"max_download_size={self.max_download_size}"
        )

    async def _validate_redirect(self, source_url: str, next_url: str) -> None:
        if self._egress_policy is not None:
            validated_next = await self._egress_policy.validate_url(next_url)
        else:
            validated_next = await validate_url_async(next_url)
        if self._redirect_policy is not None and canonical_origin(source_url) != canonical_origin(
            validated_next
        ):
            await self._redirect_policy(source_url, validated_next)

    async def _on_request_hook(self, request: httpx.Request) -> None:
        """Drop caller-supplied custom headers when scheme, host, or port changes on redirect."""
        guard = _CUSTOM_HEADER_GUARD.get()
        if guard is None:
            return
        initial_origin, custom_keys = guard
        if not custom_keys or initial_origin is None:
            return
        if canonical_origin(str(request.url)) != initial_origin:
            for key in custom_keys:
                if key in self._client.headers:
                    request.headers[key] = self._client.headers[key]
                else:
                    request.headers.pop(key, None)

    async def _on_response_hook(self, response: httpx.Response) -> None:
        """Validate every redirect hop against SSRF/robots and reject oversized Content-Length."""
        if response.is_redirect:
            location = _extract_header_case_insensitive(response.headers, "location")
            if location:
                source_url = str(response.url)
                next_url = str(httpx.URL(source_url).join(location))
                await self._validate_redirect(source_url, next_url)
            return

        if response.status_code < 400:
            cl_val = _parse_content_length(response.headers)
            if cl_val is not None and cl_val > self.max_download_size:
                raise ContentTooLargeError(
                    max_size=self.max_download_size,
                    actual_size=cl_val,
                )

    def _ensure_client_invariants(self) -> None:
        req_hooks = self._client.event_hooks.setdefault("request", [])
        if self._on_request_hook not in req_hooks:
            req_hooks.append(self._on_request_hook)
        hooks = self._client.event_hooks.setdefault("response", [])
        if self._on_response_hook not in hooks:
            hooks.append(self._on_response_hook)
        if isinstance(self._client, httpx.AsyncClient):
            self._client._cookies = make_stateless_cookies()

    async def _stream_and_read(
        self,
        url: str,
        req_headers: dict[str, str] | None,
        *,
        require_text_convertible: bool,
        allow_ocr_images: bool,
        target_format: str,
    ) -> tuple[bytes, ResponseMetadata]:
        stream_cm = (
            self._client.stream("GET", url, headers=req_headers)
            if req_headers is not None
            else self._client.stream("GET", url)
        )
        guard_token: (
            contextvars.Token[tuple[tuple[str, str, int] | None, frozenset[str]] | None] | None
        ) = None
        if req_headers:
            guard_token = _CUSTOM_HEADER_GUARD.set(
                (canonical_origin(url), frozenset(k.lower() for k in req_headers))
            )
        try:
            async with stream_cm as response:
                if response.status_code >= 400:
                    raise HTTPClientError(
                        f"HTTP {response.status_code}: {response.reason_phrase}",
                        status_code=response.status_code,
                    )

                cl_val = _parse_content_length(response.headers)
                if cl_val is not None and cl_val > self.max_download_size:
                    raise ContentTooLargeError(max_size=self.max_download_size, actual_size=cl_val)

                resp_ct = (
                    _extract_header_case_insensitive(response.headers, "content-type") or "unknown"
                )
                if require_text_convertible and not is_text_convertible_content_type(
                    resp_ct,
                    allow_ocr_images=allow_ocr_images,
                ):
                    raise UnsupportedMediaTypeError(resp_ct, target_format=target_format)

                content = await read_bounded_response_bytes(
                    response,
                    max_bytes=self.max_download_size,
                    on_too_large=lambda actual: ContentTooLargeError(
                        max_size=self.max_download_size,
                        actual_size=actual,
                    ),
                    on_decode_error=HTTPClientError,
                )

                if require_text_convertible and not is_text_convertible_content_type(
                    resp_ct,
                    allow_ocr_images=allow_ocr_images,
                    sample_bytes=content,
                ):
                    raise UnsupportedMediaTypeError(resp_ct, target_format=target_format)
        finally:
            if guard_token is not None:
                _CUSTOM_HEADER_GUARD.reset(guard_token)

        metadata: ResponseMetadata = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "url": str(response.url),
            "size": len(content),
            "content_type": resp_ct,
            "http_version": response.http_version,
            "connection_reused": getattr(response, "is_reused", None),
        }
        return content, metadata

    async def download(
        self,
        url: str,
        headers: Mapping[str, str] | None = None,
        *,
        require_text_convertible: bool = False,
        allow_ocr_images: bool = False,
        target_format: str = "text",
    ) -> tuple[bytes, ResponseMetadata]:
        """Download content from `url` with streaming byte bounds and redirect validation."""
        safe_url = redact_url_for_log(url)
        logger.info(f"Starting download from: {safe_url}")

        try:
            req_headers = sanitize_custom_headers(headers)
            self._ensure_client_invariants()
            content, metadata = await self._stream_and_read(
                url,
                req_headers,
                require_text_convertible=require_text_convertible,
                allow_ocr_images=allow_ocr_images,
                target_format=target_format,
            )
            logger.info(
                f"Download completed: {len(content)} bytes, "
                f"status: {metadata['status_code']}, "
                f"type: {metadata['content_type']}, "
                f"http_version: {metadata['http_version']}"
            )
            return content, metadata
        except httpx.TimeoutException as exc:
            logger.error(f"Timeout downloading {safe_url}: {exc}")
            raise HTTPTimeoutError(f"Request timed out after {self.timeout}s") from exc
        except httpx.RequestError as exc:
            logger.error(f"Request error downloading {safe_url}: {exc}")
            raise HTTPClientError(f"Request failed: {exc}") from exc
        except (URLValidationError, DownloadError):
            raise
        except Exception as exc:  # noqa: BLE001
            # Map unexpected transport/stream errors to DownloadError while preserving HTTP/SSRF subclasses above.
            if exc.__class__.__name__ == "RobotsDisallowedError":
                raise
            logger.error(f"Unexpected error downloading {safe_url}: {exc}")
            raise DownloadError(f"Download failed: {exc}") from exc

    def get_connection_stats(self) -> dict[str, Any]:
        """Return HTTP client connection pool configuration and liveness status."""
        is_closed = bool(getattr(self._client, "is_closed", False))
        return {
            "status": "closed" if is_closed else "healthy",
            "http2_enabled": self.http2,
            "connection_limits": {
                "max_connections": self._limits.max_connections,
                "max_keepalive": self._limits.max_keepalive_connections,
                "keepalive_expiry": self._limits.keepalive_expiry,
            },
        }

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        await self._client.aclose()

    async def __aenter__(self) -> HTTPClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

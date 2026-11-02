"""SSRF-safe httpcore/httpx transport that pins validated IPs at TCP connect time while preserving TLS SNI."""

from __future__ import annotations

import ssl
from collections.abc import Callable, Iterable
from typing import Any

import httpcore
import httpx

from .config import Settings, get_settings
from .validation import SSRFProtectionError, resolve_and_validate_host_async


class SSRFSafeNetworkBackend(httpcore.AsyncNetworkBackend):
    """AsyncNetworkBackend that resolves, validates, and pins target IPs on connect_tcp."""

    def __init__(
        self,
        inner: httpcore.AsyncNetworkBackend | None = None,
        settings_getter: Callable[[], Settings] = get_settings,
    ) -> None:
        self._inner: httpcore.AsyncNetworkBackend = inner or httpcore.AnyIOBackend()
        self._get_settings = settings_getter

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        cfg = self._get_settings()
        pinned_ips = await resolve_and_validate_host_async(host, port, cfg)
        targets = pinned_ips if pinned_ips else [host]

        last_exc: Exception | None = None
        for target_ip in targets:
            try:
                return await self._inner.connect_tcp(
                    host=target_ip,
                    port=port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except SSRFProtectionError:
                raise
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
            except OSError as exc:
                last_exc = httpcore.ConnectError(str(exc))

        assert last_exc is not None
        raise last_exc

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise SSRFProtectionError("Unix domain sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SSRFSafeHTTPTransport(httpx.AsyncHTTPTransport):
    """httpx AsyncHTTPTransport backed by SSRFSafeNetworkBackend with trust_env disabled."""

    def __init__(
        self,
        *,
        verify: ssl.SSLContext | str | bool = True,
        cert: Any = None,
        trust_env: bool = False,
        http1: bool = True,
        http2: bool = False,
        limits: httpx.Limits = httpx.Limits(max_connections=100, max_keepalive_connections=20),
        retries: int = 0,
        socket_options: Iterable[Any] | None = None,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        ssl_context = httpx.create_ssl_context(verify=verify, cert=cert, trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            max_connections=limits.max_connections,
            max_keepalive_connections=limits.max_keepalive_connections,
            keepalive_expiry=limits.keepalive_expiry,
            http1=http1,
            http2=http2,
            retries=retries,
            socket_options=socket_options,
            network_backend=backend or SSRFSafeNetworkBackend(),
        )

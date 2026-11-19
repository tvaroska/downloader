"""In-process loopback HTTP/CONNECT proxy enforcing connect-time DNS-pinned SSRF rules for Chromium."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import SplitResult, urlsplit

from ..config import get_settings
from ..validation import EgressPolicy, URLValidationError, resolve_and_validate_host_async

logger = logging.getLogger(__name__)

_HEADER_READ_LIMIT = 65536
_CONNECT_TIMEOUT_SECONDS = 15.0
_PUMP_CHUNK_BYTES = 65536
_HOP_BY_HOP_HEADERS = frozenset(
    {
        "proxy-connection",
        "proxy-authorization",
        "connection",
        "keep-alive",
    }
)


def _split_host_port(authority: str, default_port: int) -> tuple[str, int]:
    """Parse `host[:port]` or `[ipv6][:port]` authority string."""
    raw = authority.strip()
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            raise URLValidationError("Malformed IPv6 authority in browser proxy request")
        host = raw[1:end]
        rest = raw[end + 1 :]
        if rest.startswith(":"):
            port_str = rest[1:]
            if not port_str.isdigit():
                raise URLValidationError("Invalid port in browser proxy request")
            return host, int(port_str)
        return host, default_port

    if ":" in raw:
        host_part, port_part = raw.rsplit(":", 1)
        if not port_part.isdigit():
            raise URLValidationError("Invalid port in browser proxy request")
        return host_part, int(port_part)

    return raw, default_port


def _rewrite_http_request_head(
    method: str,
    version: str,
    parsed: SplitResult,
    header_lines: list[str],
) -> bytes:
    origin_path = parsed.path or "/"
    if parsed.query:
        origin_path = f"{origin_path}?{parsed.query}"

    forwarded_headers: list[str] = [f"{method} {origin_path} {version}"]
    has_host = False
    for hdr in header_lines:
        if not hdr or ":" not in hdr:
            continue
        h_name, _ = hdr.split(":", 1)
        h_lower = h_name.strip().lower()
        if h_lower in _HOP_BY_HOP_HEADERS:
            continue
        if h_lower == "host":
            has_host = True
        forwarded_headers.append(hdr)

    if not has_host:
        host_hdr = parsed.netloc or (parsed.hostname or "")
        forwarded_headers.append(f"Host: {host_hdr}")
    forwarded_headers.append("Connection: close")
    return ("\r\n".join(forwarded_headers) + "\r\n\r\n").encode("latin-1")


async def _pump_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Forward bytes until EOF or peer disconnect."""
    try:
        while True:
            data = await reader.read(_PUMP_CHUNK_BYTES)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    with contextlib.suppress(OSError, RuntimeError, ConnectionError):
        await writer.wait_closed()


class BrowserEgressProxy:
    """Loopback forward proxy that validates and IP-pins every Chromium HTTP and CONNECT hop."""

    def __init__(self, *, egress_policy: EgressPolicy | None = None) -> None:
        self._server: asyncio.Server | None = None
        self._egress_policy = egress_policy
        self.port: int = 0

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
            limit=_HEADER_READ_LIMIT,
        )
        sockets = self._server.sockets
        if not sockets:
            await self.close()
            raise RuntimeError("BrowserEgressProxy failed to bind loopback socket")
        self.port = int(sockets[0].getsockname()[1])
        logger.info(f"Started Chromium SSRF-pinning egress proxy on {self.server_url}")

    async def close(self) -> None:
        if self._server is None:
            return
        server = self._server
        self._server = None
        self.port = 0
        server.close()
        with contextlib.suppress(OSError, RuntimeError):
            await server.wait_closed()

    async def _send_error(
        self,
        writer: asyncio.StreamWriter,
        status_line: bytes,
        body: bytes = b"",
    ) -> None:
        payload = (
            status_line
            + b"\r\nContent-Type: text/plain; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        writer.write(payload)
        with contextlib.suppress(OSError, RuntimeError, ConnectionError):
            await writer.drain()

    async def _open_pinned_upstream(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not (1 <= port <= 65535):
            raise URLValidationError(f"Invalid port: {port}")
        if self._egress_policy is not None:
            validated_ips = await self._egress_policy.resolve_host(host, port)
        else:
            validated_ips = await resolve_and_validate_host_async(host, port, get_settings())
        connect_target = validated_ips[0] if validated_ips else host
        return await asyncio.wait_for(
            asyncio.open_connection(connect_target, port),
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )

    async def _forward_connect(
        self,
        target: str,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> asyncio.StreamWriter:
        host, port = _split_host_port(target, 443)
        upstream_reader, upstream_writer = await self._open_pinned_upstream(host, port)
        client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await client_writer.drain()
        await asyncio.gather(
            _pump_stream(client_reader, upstream_writer),
            _pump_stream(upstream_reader, client_writer),
        )
        return upstream_writer

    async def _forward_http(
        self,
        request_line: tuple[str, str, str],
        header_lines: list[str],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> asyncio.StreamWriter | None:
        method, target, version = request_line
        parsed = urlsplit(target)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            await self._send_error(
                client_writer,
                b"HTTP/1.1 403 Forbidden",
                b"Blocked non-HTTP scheme in browser proxy",
            )
            return None

        port = parsed.port or 80
        upstream_reader, upstream_writer = await self._open_pinned_upstream(parsed.hostname, port)
        rewritten_head = _rewrite_http_request_head(method, version, parsed, header_lines)
        upstream_writer.write(rewritten_head)
        await upstream_writer.drain()
        await asyncio.gather(
            _pump_stream(client_reader, upstream_writer),
            _pump_stream(upstream_reader, client_writer),
        )
        return upstream_writer

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            raw_head = await asyncio.wait_for(
                client_reader.readuntil(b"\r\n\r\n"),
                timeout=_CONNECT_TIMEOUT_SECONDS,
            )
            if len(raw_head) > _HEADER_READ_LIMIT:
                raise asyncio.LimitOverrunError("Request header fields too large", len(raw_head))

            lines = raw_head[:-4].decode("latin-1").split("\r\n")
            if not lines or not lines[0]:
                return
            parts = lines[0].split(" ")
            if len(parts) != 3:
                await self._send_error(client_writer, b"HTTP/1.1 400 Bad Request")
                return

            method, target, version = parts
            if method.upper() == "CONNECT":
                upstream_writer = await self._forward_connect(target, client_reader, client_writer)
            else:
                upstream_writer = await self._forward_http(
                    (method, target, version), lines[1:], client_reader, client_writer
                )
        except asyncio.LimitOverrunError:
            await self._send_error(client_writer, b"HTTP/1.1 431 Request Header Fields Too Large")
        except URLValidationError as exc:
            logger.warning(f"Blocked Chromium egress via SSRF proxy: {exc}")
            await self._send_error(
                client_writer,
                b"HTTP/1.1 403 Forbidden",
                b"Blocked by SSRF protection",
            )
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            await self._send_error(client_writer, b"HTTP/1.1 502 Bad Gateway")
        finally:
            await _close_writer(upstream_writer)
            await _close_writer(client_writer)

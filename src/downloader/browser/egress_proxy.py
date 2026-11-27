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


async def _pump_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    """Forward bytes until EOF or peer disconnect; return True if terminated by socket error."""
    had_error = False
    try:
        while True:
            data = await reader.read(_PUMP_CHUNK_BYTES)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        had_error = True
    except asyncio.CancelledError:
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if writer.can_write_eof():
                writer.write_eof()
    return had_error


def _close_writer_sync(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()


async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    _close_writer_sync(writer)
    with contextlib.suppress(OSError, RuntimeError, ConnectionError):
        await writer.wait_closed()


def _cancel_tasks(tasks: set[asyncio.Task[bool]] | tuple[asyncio.Task[bool], ...]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()


async def _pump_bidirectional(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    *,
    cancel_client_on_upstream_eof: bool,
) -> None:
    c2u = asyncio.create_task(_pump_stream(client_reader, upstream_writer))
    u2c = asyncio.create_task(_pump_stream(upstream_reader, client_writer))
    genexit = False
    try:
        done, pending = await asyncio.wait({c2u, u2c}, return_when=asyncio.FIRST_COMPLETED)
        should_cancel_peer = (
            u2c in done
            and (cancel_client_on_upstream_eof or u2c.result() or client_writer.is_closing())
        ) or (c2u in done and (c2u.result() or upstream_writer.is_closing()))
        if should_cancel_peer:
            _cancel_tasks(pending)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    except GeneratorExit:
        genexit = True
        raise
    finally:
        _cancel_tasks((c2u, u2c))
        if not genexit:
            await asyncio.gather(c2u, u2c, return_exceptions=True)


class BrowserEgressProxy:
    """Loopback forward proxy that validates and IP-pins every Chromium HTTP and CONNECT hop."""

    def __init__(self, *, egress_policy: EgressPolicy | None = None) -> None:
        self._server: asyncio.Server | None = None
        self._egress_policy = egress_policy
        self._client_tasks: set[asyncio.Task[None]] = set()
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
        server = self._server
        self._server = None
        self.port = 0
        if server is not None:
            server.close()
        active_tasks = list(self._client_tasks)
        for task in active_tasks:
            task.cancel()
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
        if server is not None:
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
    ) -> None:
        host, port = _split_host_port(target, 443)
        upstream_reader, upstream_writer = await self._open_pinned_upstream(host, port)
        genexit = False
        try:
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
            await _pump_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                cancel_client_on_upstream_eof=False,
            )
        except GeneratorExit:
            genexit = True
            raise
        finally:
            if genexit:
                _close_writer_sync(upstream_writer)
            else:
                await _close_writer(upstream_writer)

    async def _forward_http(
        self,
        request_line: tuple[str, str, str],
        header_lines: list[str],
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        method, target, version = request_line
        parsed = urlsplit(target)
        if parsed.scheme.lower() != "http" or not parsed.hostname:
            await self._send_error(
                client_writer,
                b"HTTP/1.1 403 Forbidden",
                b"Blocked non-HTTP scheme in browser proxy",
            )
            return

        port = parsed.port or 80
        upstream_reader, upstream_writer = await self._open_pinned_upstream(parsed.hostname, port)
        genexit = False
        try:
            rewritten_head = _rewrite_http_request_head(method, version, parsed, header_lines)
            upstream_writer.write(rewritten_head)
            await upstream_writer.drain()
            await _pump_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
                cancel_client_on_upstream_eof=True,
            )
        except GeneratorExit:
            genexit = True
            raise
        finally:
            if genexit:
                _close_writer_sync(upstream_writer)
            else:
                await _close_writer(upstream_writer)

    async def _dispatch_proxy_request(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
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
            await self._forward_connect(target, client_reader, client_writer)
        else:
            await self._forward_http(
                (method, target, version), lines[1:], client_reader, client_writer
            )

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._client_tasks.add(current_task)
            current_task.add_done_callback(self._client_tasks.discard)
        genexit = False
        try:
            await self._dispatch_proxy_request(client_reader, client_writer)
        except GeneratorExit:
            genexit = True
            raise
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
            if current_task is not None:
                self._client_tasks.discard(current_task)
            if genexit:
                _close_writer_sync(client_writer)
            else:
                await _close_writer(client_writer)

"""In-process loopback HTTP/CONNECT proxy enforcing connect-time DNS-pinned SSRF rules for Chromium."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from urllib.parse import urlsplit

from ..config import get_settings
from ..validation import URLValidationError, resolve_and_validate_host_async

logger = logging.getLogger(__name__)

_HEADER_READ_LIMIT = 65536
_CONNECT_TIMEOUT_SECONDS = 15.0
_PUMP_CHUNK_BYTES = 65536


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
        with contextlib.suppress(Exception):
            if writer.can_write_eof():
                writer.write_eof()


class BrowserEgressProxy:
    """Loopback forward proxy that validates and IP-pins every Chromium HTTP and CONNECT hop."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
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
        with contextlib.suppress(Exception):
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
        with contextlib.suppress(Exception):
            await writer.drain()

    async def _open_pinned_upstream(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if not (1 <= port <= 65535):
            raise URLValidationError(f"Invalid port: {port}")
        validated_ips = await resolve_and_validate_host_async(host, port, get_settings())
        connect_target = validated_ips[0] if validated_ips else host
        return await asyncio.wait_for(
            asyncio.open_connection(connect_target, port),
            timeout=_CONNECT_TIMEOUT_SECONDS,
        )

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
                await self._send_error(
                    client_writer, b"HTTP/1.1 431 Request Header Fields Too Large"
                )
                return

            lines = raw_head[:-4].decode("latin-1").split("\r\n")
            if not lines or not lines[0]:
                return
            parts = lines[0].split(" ")
            if len(parts) != 3:
                await self._send_error(client_writer, b"HTTP/1.1 400 Bad Request")
                return

            method, target, version = parts
            method_upper = method.upper()

            if method_upper == "CONNECT":
                host, port = _split_host_port(target, 443)
                upstream_reader, upstream_writer = await self._open_pinned_upstream(host, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await asyncio.gather(
                    _pump_stream(client_reader, upstream_writer),
                    _pump_stream(upstream_reader, client_writer),
                )
                return

            parsed = urlsplit(target)
            scheme = parsed.scheme.lower()
            if scheme != "http" or not parsed.hostname:
                await self._send_error(
                    client_writer,
                    b"HTTP/1.1 403 Forbidden",
                    b"Blocked non-HTTP scheme in browser proxy",
                )
                return

            port = parsed.port or 80
            upstream_reader, upstream_writer = await self._open_pinned_upstream(
                parsed.hostname, port
            )

            origin_path = parsed.path or "/"
            if parsed.query:
                origin_path = f"{origin_path}?{parsed.query}"

            forwarded_headers: list[str] = [f"{method} {origin_path} {version}"]
            has_host = False
            for hdr in lines[1:]:
                if not hdr or ":" not in hdr:
                    continue
                h_name, _ = hdr.split(":", 1)
                h_lower = h_name.strip().lower()
                if h_lower in {
                    "proxy-connection",
                    "proxy-authorization",
                    "connection",
                    "keep-alive",
                }:
                    continue
                if h_lower == "host":
                    has_host = True
                forwarded_headers.append(hdr)

            if not has_host:
                host_hdr = parsed.netloc or parsed.hostname
                forwarded_headers.append(f"Host: {host_hdr}")
            forwarded_headers.append("Connection: close")

            rewritten_head = ("\r\n".join(forwarded_headers) + "\r\n\r\n").encode("latin-1")
            upstream_writer.write(rewritten_head)
            await upstream_writer.drain()

            await asyncio.gather(
                _pump_stream(client_reader, upstream_writer),
                _pump_stream(upstream_reader, client_writer),
            )

        except URLValidationError as exc:
            logger.warning(f"Blocked Chromium egress via SSRF proxy: {exc}")
            with contextlib.suppress(Exception):
                await self._send_error(
                    client_writer,
                    b"HTTP/1.1 403 Forbidden",
                    b"Blocked by SSRF protection",
                )
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, OSError):
            with contextlib.suppress(Exception):
                await self._send_error(client_writer, b"HTTP/1.1 502 Bad Gateway")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with contextlib.suppress(Exception):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with contextlib.suppress(Exception):
                await client_writer.wait_closed()

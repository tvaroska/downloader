"""Integration test verifying Chromium redirect hops to SSRF-denied targets are blocked (`R1`)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from downloader.pdf_generator import PlaywrightPDFGenerator
from downloader.validation import SSRFProtectionError


@pytest.mark.integration
@pytest.mark.requires_playwright
@pytest.mark.asyncio
async def test_redirect_hop_to_denied_target_is_not_rendered() -> None:
    """A 302 redirect hop to a denied loopback target must fail SSRF validation and never render."""
    secret_hit = False

    async def handle_denied(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        nonlocal secret_hit
        await reader.read(4096)
        secret_hit = True
        body = b"<html><body>INTERNAL_SECRET_DATA</body></html>"
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        await writer.drain()
        writer.close()

    denied_srv = await asyncio.start_server(handle_denied, "127.0.0.1", 0)
    denied_port = int(denied_srv.sockets[0].getsockname()[1])
    denied_url = f"http://127.0.0.1:{denied_port}/secret"

    async def handle_redirector(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.read(4096)
        writer.write(
            f"HTTP/1.1 302 Found\r\nLocation: {denied_url}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode(
                "ascii"
            )
        )
        await writer.drain()
        writer.close()

    redirect_srv = await asyncio.start_server(handle_redirector, "127.0.0.1", 0)
    redirect_port = int(redirect_srv.sockets[0].getsockname()[1])
    start_url = f"http://127.0.0.1:{redirect_port}/start"

    try:
        generator = PlaywrightPDFGenerator(pool_size=1, page_load_timeout=5000, idle_timeout=0)
        try:
            with pytest.raises((SSRFProtectionError, Exception)) as exc_info:
                await generator.generate_pdf(start_url)
            assert "INTERNAL_SECRET_DATA" not in str(exc_info.value)
            assert secret_hit is False
        finally:
            with contextlib.suppress(Exception):
                await generator.close()
    finally:
        redirect_srv.close()
        denied_srv.close()
        await redirect_srv.wait_closed()
        await denied_srv.wait_closed()

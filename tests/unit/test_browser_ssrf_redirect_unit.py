"""Hermetic unit tests for browser redirect SSRF re-validation, service_workers block, and egress proxy (`R1`, `R82`, `R98`)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from downloader.browser.egress_proxy import BrowserEgressProxy
from downloader.browser.manager import BrowserPool
from downloader.browser.renderer import render_html_with_playwright
from downloader.pdf_generator import (
    BrowserSession,
    PlaywrightPDFGenerator,
    install_ssrf_route_guard,
    validate_browser_navigation,
)
from downloader.ports import RobotsDisallowedError
from downloader.validation import SSRFProtectionError


@pytest.mark.unit
@pytest.mark.asyncio
class TestBrowserSSRFRedirectAndContextHardening:
    async def test_browser_contexts_block_service_workers(self) -> None:
        pool = BrowserPool()
        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=AsyncMock())

        await pool.create_context(mock_browser, user_agent="TestUA/1.0")
        assert mock_browser.new_context.call_args.kwargs["service_workers"] == "block"

        gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0)
        fake_context = AsyncMock()
        fake_context.new_page = AsyncMock(return_value=AsyncMock())
        fake_context.route = AsyncMock()
        fake_context.on = MagicMock()
        fake_context.close = AsyncMock()

        fake_pool = AsyncMock()
        fake_pool.get_browser = AsyncMock(return_value=mock_browser)
        fake_pool.create_context = AsyncMock(side_effect=pool.create_context)
        fake_pool.release_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=fake_context)
        gen.pool = fake_pool

        async with BrowserSession(gen):
            assert mock_browser.new_context.call_args.kwargs["service_workers"] == "block"

    async def test_validate_browser_navigation_blocks_redirect_hop_to_denied_target(self) -> None:
        first_hop = SimpleNamespace(url="https://example.com/start", redirected_from=None)
        denied_hop = SimpleNamespace(
            url="http://169.254.169.254/latest/meta-data/",
            redirected_from=first_hop,
        )
        response = SimpleNamespace(
            status=200,
            url="http://169.254.169.254/latest/meta-data/",
            request=denied_hop,
        )
        page = SimpleNamespace(url="http://169.254.169.254/latest/meta-data/")

        with pytest.raises(SSRFProtectionError):
            await validate_browser_navigation(page, response)

    async def test_render_html_with_playwright_rejects_302_redirect_to_loopback(self) -> None:
        first_req = SimpleNamespace(url="https://example.com/public", redirected_from=None)
        second_req = SimpleNamespace(
            url="http://127.0.0.1:8080/internal", redirected_from=first_req
        )
        nav_response = SimpleNamespace(
            status=200,
            url="http://127.0.0.1:8080/internal",
            request=second_req,
        )

        mock_page = AsyncMock()
        mock_page.url = "http://127.0.0.1:8080/internal"
        mock_page.goto = AsyncMock(return_value=nav_response)
        mock_page.content = AsyncMock(return_value="<html>secret</html>")

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.route = AsyncMock()
        mock_context.on = MagicMock()
        mock_context.close = AsyncMock()

        mock_pool = AsyncMock()
        mock_pool.get_browser = AsyncMock(return_value=MagicMock())
        mock_pool.create_context = AsyncMock(return_value=mock_context)
        mock_pool.release_browser = AsyncMock()

        mock_gen = MagicMock()
        mock_gen.pool = mock_pool
        mock_gen.ensure_started = AsyncMock(return_value=mock_pool)
        mock_gen.user_agent = None
        mock_gen.egress_policy = None
        mock_gen.redirect_policy = None

        with (
            patch(
                "downloader.pdf_generator.get_shared_pdf_generator",
                return_value=mock_gen,
            ),
            pytest.raises(SSRFProtectionError),
        ):
            await render_html_with_playwright("https://example.com/public")

        mock_page.content.assert_not_awaited()

    async def test_blocked_redirect_maps_to_validation_error_before_status_check(self) -> None:
        """R82 & R98: Denied redirect hop with proxy 403 status raises SSRFProtectionError on PDF & HTML."""
        hop1 = SimpleNamespace(url="https://example.com/start", redirected_from=None)
        hop2_denied = SimpleNamespace(url="http://127.0.0.1:9000/secret", redirected_from=hop1)
        hop3 = SimpleNamespace(url="https://example.com/final", redirected_from=hop2_denied)
        proxy_403_resp = SimpleNamespace(
            status=403,
            status_text="Forbidden",
            url="https://example.com/final",
            request=hop3,
        )

        mock_page = AsyncMock()
        mock_page.url = "https://example.com/final"
        mock_page.goto = AsyncMock(return_value=proxy_403_resp)
        mock_page.pdf = AsyncMock(return_value=b"%PDF-1.4")
        mock_page.content = AsyncMock(return_value="<html>nope</html>")

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.route = AsyncMock()
        mock_context.on = MagicMock()
        mock_context.close = AsyncMock()

        mock_pool = AsyncMock()
        mock_pool.get_browser = AsyncMock(return_value=MagicMock())
        mock_pool.create_context = AsyncMock(return_value=mock_context)
        mock_pool.release_browser = AsyncMock()

        gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0)
        gen.pool = mock_pool

        with pytest.raises(SSRFProtectionError):
            await gen.generate_pdf("https://example.com/start")
        mock_page.pdf.assert_not_awaited()

        with pytest.raises(SSRFProtectionError):
            await render_html_with_playwright("https://example.com/start", generator=gen)
        mock_page.content.assert_not_awaited()

        # Cross-origin browser redirect also invokes redirect_policy (robots.txt check).
        cross_origin_hop = SimpleNamespace(
            url="https://example.org/disallowed",
            redirected_from=SimpleNamespace(url="https://example.com/start", redirected_from=None),
        )
        ok_resp = SimpleNamespace(
            status=200,
            url="https://example.org/disallowed",
            request=cross_origin_hop,
        )
        mock_page.url = "https://example.org/disallowed"
        mock_page.goto = AsyncMock(return_value=ok_resp)

        async def _deny_robots(_source: str, next_url: str) -> None:
            raise RobotsDisallowedError(next_url)

        gen_with_policy = PlaywrightPDFGenerator(
            pool_size=1, idle_timeout=0, redirect_policy=_deny_robots
        )
        gen_with_policy.pool = mock_pool
        with pytest.raises(RobotsDisallowedError):
            await gen_with_policy.generate_pdf("https://example.com/start")

    async def test_subresource_redirect_violation_is_blocked_on_context(self) -> None:
        listeners: dict[str, object] = {}

        class _FakeContext:
            def __init__(self) -> None:
                self.route = AsyncMock()

            def on(self, event: str, handler: object) -> None:
                listeners[event] = handler

        ctx = _FakeContext()
        await install_ssrf_route_guard(ctx)
        assert "request" in listeners

        initial_sub = SimpleNamespace(url="https://example.com/img", redirected_from=None)
        redirected_sub = SimpleNamespace(
            url="http://100.64.0.1/internal-img",
            redirected_from=initial_sub,
        )
        handler = listeners["request"]
        assert callable(handler)
        handler(redirected_sub)

        page = SimpleNamespace(url="https://example.com/article")
        resp = SimpleNamespace(
            status=200,
            url="https://example.com/article",
            request=SimpleNamespace(url="https://example.com/article", redirected_from=None),
        )
        with pytest.raises(SSRFProtectionError):
            await validate_browser_navigation(page, resp, ctx)

    async def test_egress_proxy_blocks_http_connect_and_oversized_headers(self) -> None:
        proxy = BrowserEgressProxy()
        await proxy.start()
        try:
            # 1. Plain HTTP proxy request to cloud metadata IP -> 403 Forbidden
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n"
                b"Host: 169.254.169.254\r\n\r\n"
            )
            await writer.drain()
            raw_resp = await reader.read(4096)
            writer.close()
            await writer.wait_closed()
            assert raw_resp.startswith(b"HTTP/1.1 403 Forbidden")

            # 2. HTTPS CONNECT tunnel to shared address space 100.64.0.1 -> 403 Forbidden
            reader2, writer2 = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer2.write(b"CONNECT 100.64.0.1:443 HTTP/1.1\r\nHost: 100.64.0.1:443\r\n\r\n")
            await writer2.drain()
            raw_resp2 = await reader2.read(4096)
            writer2.close()
            await writer2.wait_closed()
            assert raw_resp2.startswith(b"HTTP/1.1 403 Forbidden")

            # 3. Oversized 70 KB header block -> 431 Request Header Fields Too Large (`R109`)
            reader3, writer3 = await asyncio.open_connection("127.0.0.1", proxy.port)
            huge_header = (
                b"GET http://example.com/ HTTP/1.1\r\nX-Pad: " + (b"a" * 70_000) + b"\r\n\r\n"
            )
            writer3.write(huge_header)
            await writer3.drain()
            raw_resp3 = await reader3.read(4096)
            writer3.close()
            await writer3.wait_closed()
            assert raw_resp3.startswith(b"HTTP/1.1 431")
        finally:
            await proxy.close()

    async def test_egress_proxy_forwards_http_and_connect_and_handles_edge_cases(self) -> None:
        """Cover CONNECT tunnel, HTTP forwarding, hop-by-hop stripping, authority parsing, and errors (`R80`)."""
        from urllib.parse import urlsplit

        from downloader.browser.egress_proxy import (
            _rewrite_http_request_head,
            _split_host_port,
        )
        from downloader.config import Settings
        from downloader.validation import EgressPolicy, URLValidationError

        # Authority parser & request head rewriter
        assert _split_host_port("[::1]:8443", 443) == ("::1", 8443)
        assert _split_host_port("[::1]", 443) == ("::1", 443)
        assert _split_host_port("example.com", 80) == ("example.com", 80)
        with pytest.raises(URLValidationError):
            _split_host_port("[::1", 443)
        with pytest.raises(URLValidationError):
            _split_host_port("[::1]:abc", 443)
        with pytest.raises(URLValidationError):
            _split_host_port("example.com:abc", 80)

        rewritten = _rewrite_http_request_head(
            "GET",
            "HTTP/1.1",
            urlsplit("http://example.com/a?x=1"),
            ["Proxy-Connection: keep-alive", "Proxy-Authorization: Basic abc", "X-Keep: yes"],
        ).decode("latin-1")
        assert rewritten.startswith("GET /a?x=1 HTTP/1.1\r\n")
        assert "Proxy-Connection" not in rewritten
        assert "Proxy-Authorization" not in rewritten
        assert "X-Keep: yes" in rewritten
        assert "Host: example.com" in rewritten

        # Local echo/HTTP upstream + allow-loopback policy for hermetic proxy forwarding
        received_http_heads: list[bytes] = []

        async def _echo_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            data = await reader.read(4096)
            received_http_heads.append(data)
            if data.startswith(b"GET "):
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            else:
                writer.write(b"ECHO:" + data)
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        upstream_srv = await asyncio.start_server(_echo_upstream, "127.0.0.1", 0)
        assert upstream_srv.sockets
        up_port = upstream_srv.sockets[0].getsockname()[1]

        class _AllowLoopbackPolicy(EgressPolicy):
            async def resolve_host(self, hostname: str, port: int | None = None) -> list[str]:
                if hostname == "allowed.test":
                    return ["127.0.0.1"]
                return await super().resolve_host(hostname, port)

        proxy = BrowserEgressProxy(egress_policy=_AllowLoopbackPolicy(Settings()))
        await proxy.start()
        await proxy.start()  # idempotent start
        assert proxy.server_url == f"http://127.0.0.1:{proxy.port}"
        try:
            # 1. Plain HTTP forward + hop-by-hop header stripping
            r1, w1 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w1.write(
                f"GET http://allowed.test:{up_port}/hello?k=1 HTTP/1.1\r\n"
                "Host: allowed.test\r\n"
                "Proxy-Connection: keep-alive\r\n"
                "X-Custom: 42\r\n\r\n".encode("ascii")
            )
            await w1.drain()
            resp1 = await r1.read(4096)
            w1.close()
            await w1.wait_closed()
            assert b"200 OK" in resp1 and resp1.endswith(b"OK")
            assert b"Proxy-Connection" not in received_http_heads[-1]
            assert b"X-Custom: 42" in received_http_heads[-1]

            # 2. CONNECT tunnel to loopback echo server
            r2, w2 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w2.write(
                f"CONNECT allowed.test:{up_port} HTTP/1.1\r\nHost: allowed.test\r\n\r\n".encode(
                    "ascii"
                )
            )
            await w2.drain()
            connect_hdr = await r2.readuntil(b"\r\n\r\n")
            assert connect_hdr.startswith(b"HTTP/1.1 200 Connection Established")
            w2.write(b"ping")
            await w2.drain()
            w2.write_eof()
            tunnel_body = await r2.read(4096)
            w2.close()
            await w2.wait_closed()
            assert tunnel_body == b"ECHO:ping"

            # 3. Malformed request line -> 400 Bad Request
            r3, w3 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w3.write(b"BADLINE\r\n\r\n")
            await w3.drain()
            resp3 = await r3.read(4096)
            w3.close()
            await w3.wait_closed()
            assert resp3.startswith(b"HTTP/1.1 400 Bad Request")

            # 4. Non-HTTP scheme -> 403 Forbidden
            r4, w4 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w4.write(b"GET ftp://allowed.test/file HTTP/1.1\r\nHost: allowed.test\r\n\r\n")
            await w4.drain()
            resp4 = await r4.read(4096)
            w4.close()
            await w4.wait_closed()
            assert resp4.startswith(b"HTTP/1.1 403 Forbidden")

            # 5. Invalid port 0 -> 403 Forbidden (URLValidationError)
            r5, w5 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w5.write(b"CONNECT allowed.test:0 HTTP/1.1\r\nHost: allowed.test:0\r\n\r\n")
            await w5.drain()
            resp5 = await r5.read(4096)
            w5.close()
            await w5.wait_closed()
            assert resp5.startswith(b"HTTP/1.1 403 Forbidden")
        finally:
            await proxy.close()
            await proxy.close()  # idempotent close
            upstream_srv.close()
            await upstream_srv.wait_closed()

    async def test_egress_proxy_client_disconnect_gc_and_close_drain_cleanly(self) -> None:
        """`R118`: Client disconnect + `gc.collect()` mid-tunnel and `close()` emit zero GC/GeneratorExit errors."""
        import gc

        from downloader.config import Settings
        from downloader.validation import EgressPolicy

        loop = asyncio.get_running_loop()
        loop_exceptions: list[dict[str, object]] = []
        prev_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _lp, ctx: loop_exceptions.append(ctx))

        upstream_connected = asyncio.Event()
        upstream_closed_events = [asyncio.Event(), asyncio.Event()]
        conn_idx = 0

        async def _holding_upstream(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            nonlocal conn_idx
            my_idx = conn_idx
            conn_idx += 1
            upstream_connected.set()
            try:
                while await reader.read(64):
                    pass
            finally:
                if my_idx < len(upstream_closed_events):
                    upstream_closed_events[my_idx].set()
                if not writer.is_closing():
                    writer.close()

        upstream_srv = await asyncio.start_server(_holding_upstream, "127.0.0.1", 0)
        assert upstream_srv.sockets
        up_port = upstream_srv.sockets[0].getsockname()[1]

        class _AllowLoopbackPolicy(EgressPolicy):
            async def resolve_host(self, hostname: str, port: int | None = None) -> list[str]:
                if hostname == "allowed.test":
                    return ["127.0.0.1"]
                return await super().resolve_host(hostname, port)

        proxy = BrowserEgressProxy(egress_policy=_AllowLoopbackPolicy(Settings()))
        await proxy.start()
        w2: asyncio.StreamWriter | None = None
        try:
            # 1. Establish CONNECT tunnel, abruptly drop client + references, run gc.collect()
            r1, w1 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w1.write(
                f"CONNECT allowed.test:{up_port} HTTP/1.1\r\nHost: allowed.test\r\n\r\n".encode(
                    "ascii"
                )
            )
            await w1.drain()
            hdr = await r1.readuntil(b"\r\n\r\n")
            assert hdr.startswith(b"HTTP/1.1 200 Connection Established")
            await asyncio.wait_for(upstream_connected.wait(), timeout=2.0)
            assert len(proxy._client_tasks) == 1

            w1.close()
            await w1.wait_closed()
            del r1, w1
            gc.collect()
            await asyncio.wait_for(upstream_closed_events[0].wait(), timeout=1.0)
            gc.collect()

            # 2. Establish a second active tunnel and call proxy.close() while client stays open
            upstream_connected.clear()
            r2, w2 = await asyncio.open_connection("127.0.0.1", proxy.port)
            w2.write(
                f"CONNECT allowed.test:{up_port} HTTP/1.1\r\nHost: allowed.test\r\n\r\n".encode(
                    "ascii"
                )
            )
            await w2.drain()
            hdr2 = await r2.readuntil(b"\r\n\r\n")
            assert hdr2.startswith(b"HTTP/1.1 200 Connection Established")
            await asyncio.wait_for(upstream_connected.wait(), timeout=2.0)
            assert len(proxy._client_tasks) == 1

            await proxy.close()
            assert len(proxy._client_tasks) == 0
            await asyncio.wait_for(upstream_closed_events[1].wait(), timeout=1.0)
            gc.collect()
            await asyncio.sleep(0.02)

            assert loop_exceptions == [], f"Unexpected asyncio loop errors: {loop_exceptions}"

            # 3. Direct GeneratorExit injection on _handle_client coroutine never raises RuntimeError
            fake_writer = MagicMock()
            fake_writer.is_closing.return_value = False
            coro = proxy._handle_client(asyncio.StreamReader(), fake_writer)
            coro.close()
        finally:
            if w2 is not None and not w2.is_closing():
                w2.close()
            loop.set_exception_handler(prev_handler)
            await proxy.close()
            upstream_srv.close()
            await upstream_srv.wait_closed()

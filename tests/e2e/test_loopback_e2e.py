"""Zero-internet loopback E2E tests running real HTTP -> FastAPI -> Playwright Chromium (`R116`)."""

from __future__ import annotations

import asyncio
import gc
import logging
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

import httpx
import pytest
import uvicorn

from downloader import (
    content_converter,
    dependencies,
    main,
    pdf_generator,
    ssrf_transport,
    validation,
)
from downloader.browser import egress_proxy
from downloader.config import Settings, reload_settings
from downloader.validation import EgressPolicy, _parse_and_normalize_url

_FIXTURE_HOST = "fixture.example.test"
_E2E_API_KEY = "e2e-loopback-secret-key"

_SPA_HTML = """<!DOCTYPE html>
<html>
<head><title>Loading...</title></head>
<body>
  <div id="app">Loading...</div>
  <script>
    const ogTitle = document.createElement("meta");
    ogTitle.setAttribute("property", "og:title");
    ogTitle.setAttribute("content", "Loopback SPA Title");
    document.head.appendChild(ogTitle);
    const ogDesc = document.createElement("meta");
    ogDesc.setAttribute("property", "og:description");
    ogDesc.setAttribute("content", "Hydrated loopback description");
    document.head.appendChild(ogDesc);
    document.getElementById("app").innerHTML =
      "<main><h1>Loopback SPA Title</h1>" +
      "<p>Hydrated loopback content for HTML, Markdown, and PDF.</p></main>";
  </script>
</body>
</html>
"""

_MODAL_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Modal Page">
  <meta property="og:description" content="Modal dismissal E2E page">
</head>
<body>
  <button class="modal-close" style="display:none">Hidden 1</button>
  <button class="modal-close" hidden>Hidden 2</button>
  <button aria-label="Close" style="visibility:hidden">Hidden 3</button>
  <button title="Close" style="display:none">Hidden 4</button>
  <button aria-label="close" hidden>Hidden 5</button>
  <button title="close" style="display:none">Hidden 6</button>
  <button class="close-button" style="display:none">Hidden 7</button>
  <button class="modal-close" style="display:none">Hidden 8</button>
  <div id="promo-overlay">
    <button
      class="modal-close"
      onclick="this.parentElement.remove(); document.body.setAttribute('data-modal-dismissed', 'true');"
    >Dismiss</button>
  </div>
  <main><h1>Modal Page</h1><p>Readable article under modals.</p></main>
</body>
</html>
"""

_HIDDEN_ONLY_MODALS_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta property="og:title" content="Hidden Modals Only">
  <meta property="og:description" content="All modal buttons hidden">
</head>
<body>
  <button aria-label="close" style="display:none">H1</button>
  <button title="Close" hidden>H2</button>
  <button aria-label="Close" style="visibility:hidden">H3</button>
  <button title="close" style="display:none">H4</button>
  <button class="modal-close" hidden>H5</button>
  <button class="close-button" style="display:none">H6</button>
  <main><p>No visible modals to click.</p></main>
</body>
</html>
"""

_INTERACTIVE_HTML = """<!DOCTYPE html>
<html>
<head><title>Interactive Fixture</title></head>
<body>
  <input id="item-input" type="text" />
  <button
    id="submit-btn"
    type="button"
    onclick="
      const out = document.getElementById('output');
      out.textContent = 'Applied: ' + document.getElementById('item-input').value;
      out.setAttribute('data-applied', 'yes');
    "
  >Apply</button>
  <div id="output">pending</div>
</body>
</html>
"""


class _LoopbackFixtureEgressPolicy(EgressPolicy):
    """Allow only the local E2E fixture hostname while enforcing all real SSRF rules."""

    def __init__(self, cfg: Settings, allowed_hosts: frozenset[str]) -> None:
        super().__init__(cfg)
        self._allowed_hosts = allowed_hosts

    async def validate_url(self, url: str) -> str:
        normalized, hostname, _port = _parse_and_normalize_url(url)
        if hostname.rstrip(".").lower() in self._allowed_hosts:
            return normalized
        return await super().validate_url(url)

    async def resolve_host(self, hostname: str, port: int | None = None) -> list[str]:
        if hostname.rstrip(".").lower() in self._allowed_hosts:
            return ["127.0.0.1"]
        return await super().resolve_host(hostname, port)


class _LoopbackFixtureServer:
    """Minimal asyncio HTTP/1.1 fixture server bound to loopback."""

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self.port: int = 0
        self.secret_hits: int = 0

    def _build_response(self, path: str) -> tuple[str, list[str], bytes]:
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\nDisallow: /blocked-by-robots\n"
            return "200 OK", ["Content-Type: text/plain; charset=utf-8"], body
        if path == "/redirect-to-loopback":
            loc = f"http://127.0.0.1:{self.port}/secret"
            return "302 Found", [f"Location: {loc}"], b""
        if path == "/secret":
            self.secret_hits += 1
            return (
                "200 OK",
                ["Content-Type: text/html; charset=utf-8"],
                b"<html><body>INTERNAL_SECRET_MUST_NOT_LEAK</body></html>",
            )
        routes: dict[str, str] = {
            "/spa-page": _SPA_HTML,
            "/modal-page": _MODAL_HTML,
            "/hidden-only-modals": _HIDDEN_ONLY_MODALS_HTML,
            "/interactive": _INTERACTIVE_HTML,
            "/blocked-by-robots": "<html><body><p>Disallowed</p></body></html>",
        }
        html = routes.get(path)
        if html is not None:
            return "200 OK", ["Content-Type: text/html; charset=utf-8"], html.encode("utf-8")
        return "404 Not Found", ["Content-Type: text/plain; charset=utf-8"], b"Not Found"

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, OSError):
            writer.close()
            return
        request_line = raw.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        parts = request_line.split(" ")
        raw_target = parts[1] if len(parts) >= 2 else "/"
        path = raw_target.split("?", 1)[0]
        status_line, extra_headers, body = self._build_response(path)
        headers = [
            f"HTTP/1.1 {status_line}",
            *extra_headers,
            f"Content-Length: {len(body)}",
            "Connection: close",
        ]
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
        with suppress(OSError, ConnectionError):
            await writer.drain()
        writer.close()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None


class _ErrorCapturingHandler(logging.Handler):
    """Capture asyncio errors and orphaned Playwright/Future teardown warnings."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        is_orphaned_future = (
            "Future exception was never retrieved" in msg
            or "Task exception was never retrieved" in msg
            or "TargetClosedError" in msg
        )
        if is_orphaned_future or (record.name == "asyncio" and record.levelno >= logging.ERROR):
            self.messages.append(f"{record.name} [{record.levelname}]: {msg}")


class _AsyncioLoopMonitor:
    """Fail if the active event loop receives any unhandled exception or orphaned future."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._prev_handler = loop.get_exception_handler()
        self._loop_errors: list[str] = []
        self._log_handler = _ErrorCapturingHandler()
        self._watched_loggers = (
            logging.getLogger("asyncio"),
            logging.getLogger("playwright"),
            logging.getLogger("downloader"),
        )

    def __enter__(self) -> _AsyncioLoopMonitor:
        def _on_loop_error(
            _loop: asyncio.AbstractEventLoop,
            context: dict[str, Any],
        ) -> None:
            msg = str(context.get("message", ""))
            exc = context.get("exception")
            self._loop_errors.append(f"{msg}: {exc!r}")

        self._loop.set_exception_handler(_on_loop_error)
        for watched in self._watched_loggers:
            watched.addHandler(self._log_handler)
        return self

    async def assert_clean(self) -> None:
        gc.collect()
        await asyncio.sleep(0.05)
        gc.collect()
        self._loop.set_exception_handler(self._prev_handler)
        for watched in self._watched_loggers:
            watched.removeHandler(self._log_handler)
        assert self._loop_errors == [], f"Unhandled asyncio loop errors: {self._loop_errors}"
        assert self._log_handler.messages == [], (
            f"Orphaned Future/Playwright teardown logs detected: {self._log_handler.messages}"
        )


def _configure_prod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DOWNLOADER_KEY", _E2E_API_KEY)
    monkeypatch.delenv("REDIS_URI", raising=False)
    monkeypatch.setenv("PDF_POOL_SIZE", "1")
    monkeypatch.setenv("PDF_CONCURRENCY", "1")
    monkeypatch.setenv("PDF_IDLE_TIMEOUT", "0")
    monkeypatch.setenv("RESPECT_ROBOTS_TXT", "true")
    monkeypatch.setenv("RATELIMIT_ENABLED", "true")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    reload_settings()
    content_converter._js_heavy_cache.clear()
    content_converter._static_html_cache.clear()


def _bind_loopback_socket() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    sock.setblocking(False)
    return sock, int(sock.getsockname()[1])


class _NoSignalUvicornServer(uvicorn.Server):
    """Run Uvicorn on the test event loop without overriding pytest signal handlers."""

    def install_signal_handlers(self) -> None:
        return None


@asynccontextmanager
async def _running_loopback_app(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[str, str, _LoopbackFixtureServer, dict[str, str]]]:
    _configure_prod_env(monkeypatch)
    fixture_srv = _LoopbackFixtureServer()
    await fixture_srv.start()

    allowed = frozenset({_FIXTURE_HOST})
    orig_resolve = validation.resolve_and_validate_host_async

    async def _resolve_host_or_fixture(
        hostname: str,
        port: int | None = None,
        settings: Settings | None = None,
    ) -> list[str]:
        if hostname.rstrip(".").lower() in allowed:
            return ["127.0.0.1"]
        return await orig_resolve(hostname, port, settings)

    for resolver_mod in (validation, ssrf_transport, egress_proxy):
        monkeypatch.setattr(
            resolver_mod, "resolve_and_validate_host_async", _resolve_host_or_fixture
        )
    for policy_mod in (main, dependencies, pdf_generator):
        monkeypatch.setattr(
            policy_mod,
            "EgressPolicy",
            lambda cfg: _LoopbackFixtureEgressPolicy(cfg, allowed),
        )

    sock, app_port = _bind_loopback_socket()
    config = uvicorn.Config(
        main.app,
        host="127.0.0.1",
        port=app_port,
        lifespan="on",
        log_level="warning",
    )
    server = _NoSignalUvicornServer(config)

    monitor = _AsyncioLoopMonitor(asyncio.get_running_loop())
    monitor.__enter__()
    server_task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if server_task.done():
                await server_task
                raise AssertionError("uvicorn server exited before becoming ready")
            await asyncio.sleep(0.02)

        app_base = f"http://127.0.0.1:{app_port}"
        fixture_base = f"http://{_FIXTURE_HOST}:{fixture_srv.port}"
        auth_headers = {"Authorization": f"Bearer {_E2E_API_KEY}"}
        yield app_base, fixture_base, fixture_srv, auth_headers
    finally:
        server.should_exit = True
        with suppress(TimeoutError):
            await asyncio.wait_for(server_task, timeout=20.0)
        if not server_task.done():
            server_task.cancel()
            with suppress(asyncio.CancelledError):
                await server_task
        await fixture_srv.close()
        reload_settings()
        await monitor.assert_clean()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_loopback_prod_health_docs_and_format_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify no-Redis production startup and live GET /{url} HTML, Markdown, and PDF rendering."""
    async with _running_loopback_app(monkeypatch) as (app_base, fixture_base, _srv, auth):
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            health = await client.get(f"{app_base}/health")
            assert health.status_code == 200
            health_json = health.json()
            assert health_json["status"] == "healthy"
            assert health_json["environment"] == "production"
            assert health_json["auth_enabled"] is True
            assert health_json["services"]["job_manager"]["status"] == "disabled"
            assert health_json["services"]["pdf_generation"]["max_concurrent_pdfs"] == 1

            assert (await client.get(f"{app_base}/docs")).status_code == 404
            assert (await client.get(f"{app_base}/openapi.json")).status_code == 404
            assert (await client.get(f"{app_base}/metrics")).status_code == 401
            assert (await client.get(f"{app_base}/metrics", headers=auth)).status_code == 200

            spa_url = f"{fixture_base}/spa-page"

            html_resp = await client.get(
                f"{app_base}/{spa_url}",
                headers={**auth, "Accept": "text/html"},
            )
            assert html_resp.status_code == 200
            assert html_resp.headers.get("X-Rendered-With-JS") == "true"
            assert "charset=utf-8" in html_resp.headers.get("content-type", "").lower()
            assert "Hydrated loopback content for HTML, Markdown, and PDF." in html_resp.text

            md_resp = await client.get(
                f"{app_base}/{spa_url}",
                headers={**auth, "Accept": "text/markdown"},
            )
            assert md_resp.status_code == 200
            assert md_resp.headers.get("X-Rendered-With-JS") == "true"
            assert "# Loopback SPA Title" in md_resp.text
            assert "Hydrated loopback content for HTML, Markdown, and PDF." in md_resp.text

            pdf_resp = await client.get(
                f"{app_base}/{spa_url}",
                headers={**auth, "Accept": "application/pdf"},
            )
            assert pdf_resp.status_code == 200
            assert "application/pdf" in pdf_resp.headers.get("content-type", "").lower()
            assert pdf_resp.content.startswith(b"%PDF-")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_loopback_modal_dismissal_render_actions_and_ssrf_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify hidden modal skip, POST /render actions, robots.txt, SSRF blocks, and clean teardown."""
    async with _running_loopback_app(monkeypatch) as (app_base, fixture_base, srv, auth):
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            modal_resp = await client.get(
                f"{app_base}/{fixture_base}/modal-page?render=true",
                headers={**auth, "Accept": "text/html"},
            )
            assert modal_resp.status_code == 200
            assert modal_resp.headers.get("X-Rendered-With-JS") == "true"
            assert 'data-modal-dismissed="true"' in modal_resp.text
            assert 'id="promo-overlay"' not in modal_resp.text
            assert "Readable article under modals." in modal_resp.text

            hidden_resp = await client.get(
                f"{app_base}/{fixture_base}/hidden-only-modals?render=true",
                headers={**auth, "Accept": "text/html"},
            )
            assert hidden_resp.status_code == 200
            assert "No visible modals to click." in hidden_resp.text

            actions_payload: dict[str, Any] = {
                "url": f"{fixture_base}/interactive",
                "output": "html",
                "actions": [
                    {"type": "fill", "selector": "#item-input", "value": "Loopback OK"},
                    {"type": "click", "selector": "#submit-btn"},
                    {"type": "wait_for_selector", "selector": '#output[data-applied="yes"]'},
                ],
            }
            render_html = await client.post(
                f"{app_base}/render",
                headers=auth,
                json=actions_payload,
            )
            assert render_html.status_code == 200
            assert "Applied: Loopback OK" in render_html.text

            render_pdf = await client.post(
                f"{app_base}/render",
                headers=auth,
                json={**actions_payload, "output": "pdf"},
            )
            assert render_pdf.status_code == 200
            assert render_pdf.content.startswith(b"%PDF-")

            robots_resp = await client.get(
                f"{app_base}/{fixture_base}/blocked-by-robots",
                headers={**auth, "Accept": "text/html"},
            )
            assert robots_resp.status_code == 403
            assert robots_resp.json()["detail"]["error_type"] == "robots_disallowed"

            direct_ssrf = await client.get(
                f"{app_base}/http://127.0.0.1:{srv.port}/secret",
                headers={**auth, "Accept": "text/html"},
            )
            assert direct_ssrf.status_code == 400
            direct_detail = direct_ssrf.json()["detail"]
            assert direct_detail["error_type"] == "validation_error"
            assert "loopback" in direct_detail["error"].lower()

            redirect_ssrf = await client.get(
                f"{app_base}/{fixture_base}/redirect-to-loopback?render=true",
                headers={**auth, "Accept": "text/html"},
            )
            assert redirect_ssrf.status_code == 400
            redirect_detail = redirect_ssrf.json()["detail"]
            assert redirect_detail["error_type"] == "validation_error"
            assert "loopback" in redirect_detail["error"].lower()
            assert srv.secret_hits == 0

"""Unit tests for v0.6.1 security, single-fetch rendering, and stateful pipeline hardening."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from src.downloader.auth import verify_api_key
from src.downloader.config import (
    AuthConfig,
    BatchConfig,
    ContentConfig,
    RateLimitConfig,
    Settings,
    reload_settings,
)
from src.downloader.content_converter import (
    BoundedCache,
    SelectorTimeoutError,
    _is_js_heavy_domain,
    should_use_playwright_fallback,
    should_use_playwright_for_html,
)
from src.downloader.crawling.robots import RobotsChecker, RobotsDisallowedError
from src.downloader.crawling.throttle import DomainThrottle
from src.downloader.dependencies import (
    get_http_client,
    get_job_manager_dependency,
    get_pdf_semaphore,
    get_robots_checker,
    get_scheduler_dependency,
)
from src.downloader.http_client import HTTPClient, HTTPClientError
from src.downloader.main import app
from src.downloader.middleware import MetricsMiddleware
from src.downloader.models.responses import BatchRequest, BatchURLRequest
from src.downloader.pdf_generator import PDFGeneratorError
from src.downloader.routes.batch import process_background_batch_job, process_single_url_in_batch
from src.downloader.scheduler.executor import ScheduledJobExecutor
from src.downloader.ssrf_transport import SSRFSafeNetworkBackend
from src.downloader.validation import (
    SSRFProtectionError,
    URLValidationError,
    sanitize_custom_headers,
    validate_url,
)


@pytest.mark.unit
class TestTrackASingleFetchAndContentConfig:
    """Track A: single-fetch browser rendering, semaphore fairness, and ContentConfig wiring."""

    @pytest.mark.parametrize(
        ("accept", "expected_ct", "expected_snippet"),
        [
            ("text/html", "text/html", "Rendered Article Body"),
            ("text/plain", "text/plain", "Rendered Article Body"),
            ("text/markdown", "text/markdown", "Rendered Article Body"),
        ],
    )
    def test_single_fetch_render_true_skips_http_client(
        self,
        accept: str,
        expected_ct: str,
        expected_snippet: str,
    ) -> None:
        rendered_dom = b"<html><body><article><h1>Title</h1><p>Rendered Article Body</p></article></body></html>"
        mock_http = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.render_html_with_playwright",
                    new_callable=AsyncMock,
                    return_value=rendered_dom,
                ) as mock_render,
                TestClient(app) as client,
            ):
                resp = client.get(
                    "/https://example.com?render=true&wait_for=article",
                    headers={"Accept": accept},
                )
                assert resp.status_code == 200
                assert expected_ct in resp.headers["content-type"]
                assert resp.headers["X-Rendered-With-JS"] == "true"
                assert expected_snippet in resp.text
                mock_render.assert_called_once_with("https://example.com", "article")
                mock_http.download.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    def test_single_fetch_selector_timeout_skips_http_fallback(self) -> None:
        mock_http = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.render_html_with_playwright",
                    new_callable=AsyncMock,
                    side_effect=SelectorTimeoutError(".missing", 5000),
                ),
                TestClient(app) as client,
            ):
                resp = client.get(
                    "/https://example.com?wait_for=.missing",
                    headers={"Accept": "text/markdown"},
                )
                assert resp.status_code == 408
                assert resp.json()["detail"]["error_type"] == "selector_timeout_error"
                mock_http.download.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    def test_render_true_degradation_skips_secondary_playwright(self) -> None:
        static_html = b"<html><body><div id='root'></div><main><p>Static Fallback Text</p></main></body></html>"
        mock_http = AsyncMock()
        mock_http.download.return_value = (
            static_html,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(static_html),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.render_html_with_playwright",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("Chromium crashed"),
                ) as mock_render,
                patch(
                    "src.downloader.services.content_processor.should_use_playwright_for_html"
                ) as mock_auto_html,
                patch(
                    "src.downloader.services.content_processor._playwright_fallback_for_content",
                    new_callable=AsyncMock,
                ) as mock_empty_fb,
                TestClient(app) as client,
            ):
                resp_html = client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/html"},
                )
                assert resp_html.status_code == 200
                assert resp_html.headers["X-Rendered-With-JS"] == "false"
                assert resp_html.content == static_html
                mock_auto_html.assert_not_called()

                resp_md = client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/markdown"},
                )
                assert resp_md.status_code == 200
                assert resp_md.headers["X-Rendered-With-JS"] == "false"
                assert "Static Fallback Text" in resp_md.text
                mock_empty_fb.assert_not_called()
                assert mock_render.call_count == 2
                assert mock_http.download.call_count == 2
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    def test_multi_format_render_true_single_render_and_no_pdf_deadlock(self) -> None:
        rendered_dom = b"<html><body><main><h1>Multi Heading</h1><p>Body</p></main></body></html>"
        single_slot_sem = asyncio.Semaphore(1)
        mock_http = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_pdf_semaphore] = lambda: single_slot_sem
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.render_html_with_playwright",
                    new_callable=AsyncMock,
                    return_value=rendered_dom,
                ) as mock_render,
                patch(
                    "src.downloader.services.content_processor.generate_pdf_from_url",
                    new_callable=AsyncMock,
                    return_value=b"%PDF-1.4 fake",
                ) as mock_pdf,
                TestClient(app) as client,
            ):
                resp = client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/html, text/markdown, application/pdf"},
                )
                assert resp.status_code == 200
                payload = resp.json()
                assert "errors" not in payload
                assert "Multi Heading" in payload["text/html"]
                assert "Multi Heading" in payload["text/markdown"]
                assert payload["application/pdf"]
                mock_render.assert_called_once_with("https://example.com", None)
                mock_pdf.assert_called_once_with("https://example.com")
                mock_http.download.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_pdf_semaphore, None)

    def test_single_fetch_pdf_and_4xx_status_propagation(self) -> None:
        mock_http = AsyncMock()
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.generate_pdf_from_url",
                    new_callable=AsyncMock,
                    side_effect=PDFGeneratorError("HTTP 404: Not Found", status_code=404),
                ),
                TestClient(app) as client,
            ):
                resp = client.get(
                    "/https://example.com/missing",
                    headers={"Accept": "application/pdf"},
                )
                assert resp.status_code == 404
                assert resp.json()["detail"]["error_type"] == "http_error"
                mock_http.download.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    def test_semaphore_saturation_503_explicit_vs_200_implicit(self) -> None:
        locked_sem = asyncio.Semaphore(1)
        # Exhaust the semaphore synchronously via private counter for deterministic unit testing
        locked_sem._value = 0

        static_spa = b"<html><body><div id='root'>Static shell</div></body></html>"
        mock_http = AsyncMock()
        mock_http.download.return_value = (
            static_spa,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": len(static_spa),
                "headers": {"content-type": "text/html"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_pdf_semaphore] = lambda: locked_sem
        try:
            with (
                patch(
                    "src.downloader.services.content_processor.should_use_playwright_for_html",
                    return_value=True,
                ),
                TestClient(app) as client,
            ):
                explicit = client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/html"},
                )
                assert explicit.status_code == 503
                assert explicit.json()["detail"]["error_type"] == "service_unavailable"

                implicit = client.get(
                    "/https://example.com",
                    headers={"Accept": "text/html"},
                )
                assert implicit.status_code == 200
                assert implicit.headers["X-Rendered-With-JS"] == "false"
                assert implicit.content == static_spa
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_pdf_semaphore, None)

    def test_content_config_flags_domain_suffix_and_bounded_cache_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _is_js_heavy_domain("https://medium.com/post", ["medium.com"]) is True
        assert _is_js_heavy_domain("https://blog.medium.com/post", ["medium.com"]) is True
        assert _is_js_heavy_domain("https://notmedium.com/post", ["medium.com"]) is False
        assert _is_js_heavy_domain("https://example.com/?ref=medium.com", ["medium.com"]) is False

        disabled_settings = Settings(content=ContentConfig(enable_html_js_rendering=False))
        with patch("src.downloader.content_converter.get_settings", return_value=disabled_settings):
            html = b"<html><body><div id='root'>please enable javascript</div></body></html>"
            assert (
                should_use_playwright_for_html("https://medium.com/x", html, "text/html") is False
            )
            assert (
                should_use_playwright_fallback("https://medium.com/x", html, "text/html") is False
            )

        fake_now = 100.0
        monkeypatch.setattr("src.downloader.content_converter.time.monotonic", lambda: fake_now)
        cache = BoundedCache(maxsize=lambda: 2, ttl=lambda: 10.0)
        cache.add("a")
        fake_now = 105.0
        cache.add("b")
        assert len(cache) == 2
        # Advance past 'a' TTL (111 - 100 >= 10) while 'b' (111 - 105 < 10) stays valid
        fake_now = 111.0
        assert "a" not in cache
        assert "b" in cache
        assert len(cache) == 1

        zero_cache = BoundedCache(maxsize=0, ttl=10.0)
        zero_cache.add("x")
        assert "x" not in zero_cache
        assert len(zero_cache) == 0


@pytest.mark.unit
class TestTrackBAuthDocsRateLimitAndSSRF:
    """Track B: .env auth parity, prod OpenAPI disablement, XFF rate limits, and DNS-pinned SSRF."""

    def test_auth_config_loads_dotenv_file_and_constant_time_verify(self, tmp_path: Path) -> None:
        dotenv_path = tmp_path / ".env"
        dotenv_path.write_text("DOWNLOADER_KEY=dotenv-secret-key\n", encoding="utf-8")

        with patch.dict("os.environ", {}, clear=True):
            cfg = AuthConfig(_env_file=str(dotenv_path))
            assert cfg.api_key == "dotenv-secret-key"

            settings = Settings(auth=cfg)
            with patch("src.downloader.auth.get_settings", return_value=settings):
                assert verify_api_key("dotenv-secret-key") is True
                assert verify_api_key("wrong-key") is False

    @pytest.mark.parametrize(
        "doc_path",
        ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"],
    )
    def test_docs_disabled_in_production_and_enabled_in_development(self, doc_path: str) -> None:
        prev_settings = getattr(app.state, "settings", None)
        try:
            with TestClient(app) as client:
                app.state.settings = Settings(environment="production")
                prod_resp = client.get(doc_path)
                assert prod_resp.status_code == 404
                assert client.get("/health").status_code == 200

                app.state.settings = Settings(environment="development")
                dev_resp = client.get(doc_path)
                assert dev_resp.status_code == 200
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings

    def test_trusted_proxy_xff_resolution_and_unauth_bucket_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REDIS_URI", raising=False)
        reload_settings()

        prev_settings = getattr(app.state, "settings", None)
        test_settings = Settings(
            auth=AuthConfig(api_key="valid-token"),
            ratelimit=RateLimitConfig(
                enabled=True,
                default_limit="2/minute",
                download_limit="2/minute",
                trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
            ),
        )
        mock_http = AsyncMock()
        mock_http.download.return_value = (
            b"<html>ok</html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 15,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            # 1) Trusted ingress (127.0.0.1) resolves rightmost non-trusted XFF hop (203.0.113.5);
            # unauthenticated flood hits 401 -> 429 without starving valid API key callers!
            with TestClient(app, client=("127.0.0.1", 50000)) as trusted_client:
                app.state.settings = test_settings
                xff = {"X-Forwarded-For": "198.51.100.99, 203.0.113.5, 10.0.0.2"}
                r1 = trusted_client.get("/https://example.com", headers=xff)
                r2 = trusted_client.get("/https://example.com", headers=xff)
                r3 = trusted_client.get("/https://example.com", headers=xff)
                assert r1.status_code == 401
                assert r2.status_code == 401
                assert r3.status_code == 429

                authed_headers = {**xff, "Authorization": "Bearer valid-token"}
                ok_resp = trusted_client.get("/https://example.com", headers=authed_headers)
                assert ok_resp.status_code == 200

            # 2) Direct untrusted peer (198.51.100.77) cannot spoof X-Forwarded-For to bypass limits
            with TestClient(app, client=("198.51.100.77", 50000)) as direct_client:
                app.state.settings = test_settings
                h1 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.1",
                }
                h2 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.2",
                }
                h3 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.3",
                }
                assert direct_client.get("/https://example.com", headers=h1).status_code == 200
                assert direct_client.get("/https://example.com", headers=h2).status_code == 200
                assert direct_client.get("/https://example.com", headers=h3).status_code == 429
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    @pytest.mark.asyncio
    async def test_ssrf_safe_network_backend_pins_ip_blocks_rebinding_and_maps_oserror(
        self,
    ) -> None:
        inner = MagicMock(spec=httpcore.AsyncNetworkBackend)
        fake_stream = AsyncMock(spec=httpcore.AsyncNetworkStream)
        fake_stream.start_tls = AsyncMock(return_value=fake_stream)
        inner.connect_tcp = AsyncMock(return_value=fake_stream)

        backend = SSRFSafeNetworkBackend(inner=inner)

        # Public DNS resolution pins connect_tcp to resolved IP and preserves SNI hostname
        with patch(
            "src.downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            return_value=["93.184.215.14"],
        ):
            await backend.connect_tcp("example.com", 443, timeout=5.0)
            inner.connect_tcp.assert_awaited_once()
            assert inner.connect_tcp.await_args.kwargs["host"] == "93.184.215.14"
            assert inner.connect_tcp.await_args.kwargs["port"] == 443

        # DNS rebinding to cloud metadata IP at connect time raises SSRFProtectionError
        with patch(
            "src.downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            side_effect=SSRFProtectionError("Access to cloud metadata endpoints is blocked"),
        ):
            with pytest.raises(SSRFProtectionError):
                await backend.connect_tcp("rebind.example.com", 80)

        # Raw OSError from socket connect is converted into httpcore.ConnectError (-> HTTPClientError)
        inner.connect_tcp = AsyncMock(side_effect=OSError("Network unreachable"))
        with patch(
            "src.downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            return_value=["93.184.215.14"],
        ):
            with pytest.raises(httpcore.ConnectError):
                await backend.connect_tcp("example.com", 443)

        client = HTTPClient(timeout=2.0)
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("connect failed", request=req))
            )
        )
        try:
            with pytest.raises(HTTPClientError):
                await client.download("https://example.com")
        finally:
            await client.close()

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://[::1]/admin",
            "http://[::ffff:127.0.0.1]/admin",
            "http://[::ffff:169.254.169.254]/latest/meta-data/",
            "http://0x7f000001/",
            "http://2130706433/",
            "http://0177.0.0.1/",
            "http://127.1/",
            "http://0177.0.0.08/",
            "http://999.999.999.999/",
            "http://sub.localhost/internal",
        ],
    )
    def test_ssrf_ip_literal_and_hostname_variants_blocked(self, blocked_url: str) -> None:
        with pytest.raises(URLValidationError):
            validate_url(blocked_url)


@pytest.mark.unit
class TestTrackCSchedulesBatchAndMetrics:
    """Track C & D: schedule validation/crawling/workers, batch progress & headers, and metrics paths."""

    def test_schedule_create_rejects_ssrf_and_prohibited_headers(self) -> None:
        mock_scheduler = MagicMock()
        app.dependency_overrides[get_scheduler_dependency] = lambda: mock_scheduler
        try:
            with TestClient(app) as client:
                ssrf_resp = client.post(
                    "/schedules",
                    json={
                        "name": "Metadata Probe",
                        "url": "http://169.254.169.254/latest/meta-data/",
                        "cron_expression": "0 9 * * *",
                        "format": "text",
                    },
                )
                assert ssrf_resp.status_code == 400
                assert ssrf_resp.json()["detail"]["error_type"] == "validation_error"

                hdr_resp = client.post(
                    "/schedules",
                    json={
                        "name": "Host Spoof",
                        "url": "https://example.com/report",
                        "cron_expression": "0 9 * * *",
                        "format": "text",
                        "headers": {"Host": "internal.local"},
                    },
                )
                assert hdr_resp.status_code == 400
                assert hdr_resp.json()["detail"]["error_type"] == "validation_error"
                mock_scheduler.scheduler.add_job.assert_not_called()
        finally:
            app.dependency_overrides.pop(get_scheduler_dependency, None)

    @pytest.mark.asyncio
    async def test_scheduled_executor_robots_headers_json_size_and_worker_release(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = AsyncMock()
        http_client = AsyncMock()
        robots = AsyncMock(spec=RobotsChecker)
        throttle = AsyncMock(spec=DomainThrottle)

        # 1) Disallowed by robots.txt fails immediately after attempt 1 without retries
        robots.check_allowed.side_effect = RobotsDisallowedError("https://example.com/private")
        executor = ScheduledJobExecutor(
            http_client=http_client,
            storage=storage,
            robots_checker=robots,
            domain_throttle=throttle,
            max_workers=1,
        )
        disallowed_exec = await executor.execute(
            "sched-1", "https://example.com/private", "text", None
        )
        assert disallowed_exec.success is False
        assert disallowed_exec.attempt == 1
        http_client.download.assert_not_called()

        # 2) Allowed URL forwards custom headers (preserving casing), throttles, and serializes JSON size
        robots.check_allowed.side_effect = None
        robots.check_allowed.return_value = 0.5
        http_client.download.return_value = (
            b'{"hello":"world"}',
            {"url": "https://example.com/api", "content_type": "application/json", "size": 17},
        )
        json_exec = await executor.execute(
            "sched-2",
            "https://example.com/api",
            "json",
            {"X-Custom-Token": "abc-123"},
        )
        assert json_exec.success is True
        assert json_exec.content_size is not None and json_exec.content_size > 40
        throttle.wait_if_needed.assert_awaited_once_with("example.com", 0.5)
        assert http_client.download.await_args.kwargs["headers"] == {"X-Custom-Token": "abc-123"}

        # 3) Single-fetch PDF skips http_client.download
        http_client.download.reset_mock()
        pdf_exec_runner = ScheduledJobExecutor(
            http_client=http_client,
            storage=storage,
            pdf_semaphore=asyncio.Semaphore(1),
            robots_checker=robots,
            domain_throttle=throttle,
            max_workers=1,
        )
        with patch(
            "src.downloader.scheduler.executor.generate_pdf_from_url",
            new_callable=AsyncMock,
            return_value=b"%PDF-1.4 payload",
        ):
            pdf_exec = await pdf_exec_runner.execute(
                "sched-pdf", "https://example.com/doc", "pdf", None
            )
            assert pdf_exec.success is True
            assert pdf_exec.content_size == len(b"%PDF-1.4 payload")
            http_client.download.assert_not_called()

        # 4) Worker semaphore (max_workers=1) is released BEFORE retry backoff sleep
        observed_free_slots_during_sleep: list[int] = []

        async def fake_sleep(_delay: float) -> None:
            observed_free_slots_during_sleep.append(pdf_exec_runner._worker_semaphore._value)

        monkeypatch.setattr("src.downloader.scheduler.executor.asyncio.sleep", fake_sleep)
        http_client.download.side_effect = RuntimeError("transient upstream failure")
        failed_exec = await pdf_exec_runner.execute(
            "sched-retry", "https://example.com/flaky", "text", None
        )
        assert failed_exec.success is False
        assert failed_exec.attempt == 3
        assert observed_free_slots_during_sleep == [1, 1]

    @pytest.mark.asyncio
    async def test_batch_custom_headers_single_fetch_pdf_and_max_urls_setting(self) -> None:
        sanitized = sanitize_custom_headers({"X-Trace-Id": "trace-42"})
        assert sanitized == {"X-Trace-Id": "trace-42"}

        # Single-fetch PDF in batch skips http_client.download and propagates 404
        mock_http = AsyncMock()
        sem = asyncio.Semaphore(1)
        with patch(
            "src.downloader.routes.batch.generate_pdf_from_url",
            new_callable=AsyncMock,
            side_effect=PDFGeneratorError("HTTP 404: Not Found", status_code=404),
        ):
            res_pdf = await process_single_url_in_batch(
                url_request=BatchURLRequest(url="https://example.com/missing", format="pdf"),
                default_format="pdf",
                timeout=10,
                request_id="REQ-PDF",
                http_client=mock_http,
                pdf_semaphore=sem,
            )
            assert res_pdf.success is False
            assert res_pdf.status_code == 404
            assert res_pdf.error_type == "http_error"
            mock_http.download.assert_not_called()

        # Custom headers forwarded on non-PDF batch URL + incremental progress updates
        mock_http.download.return_value = (
            b"<html><body><p>Batch Hello</p></body></html>",
            {"url": "https://example.com", "content_type": "text/html", "size": 44},
        )
        job_manager = AsyncMock()
        job_manager.create_job.return_value = "job-123"
        job_manager.start_background_job = AsyncMock()

        batch_req = BatchRequest(
            urls=[
                BatchURLRequest(
                    url="https://example.com",
                    format="text",
                    custom_headers={"X-Api-Trace": "t1"},
                ),
                BatchURLRequest(url="https://test.com", format="html"),
            ]
        )
        results, summary = await process_background_batch_job(
            job_id="job-123",
            batch_request=batch_req,
            job_manager=job_manager,
            batch_semaphore=asyncio.Semaphore(5),
            http_client=mock_http,
            pdf_semaphore=sem,
            robots_checker=None,
            domain_throttle=None,
        )
        assert summary["successful_requests"] == 2
        assert len(results) == 2
        assert job_manager.update_job_status.call_count == 2
        first_call_kwargs = mock_http.download.await_args_list[0].kwargs
        assert first_call_kwargs["headers"] == {"X-Api-Trace": "t1"}

        # Configurable BATCH_MAX_URLS_PER_BATCH via app.state.settings + prohibited header 400
        prev_settings = getattr(app.state, "settings", None)
        app.dependency_overrides[get_job_manager_dependency] = lambda: job_manager
        try:
            with TestClient(app) as client:
                app.state.settings = Settings(batch=BatchConfig(max_urls_per_batch=60))
                urls_55 = [{"url": "https://example.com"} for _ in range(55)]
                ok_55 = client.post("/batch", json={"urls": urls_55})
                assert ok_55.status_code == 200

                bad_hdr = client.post(
                    "/batch",
                    json={
                        "urls": [
                            {
                                "url": "https://example.com",
                                "custom_headers": {"Transfer-Encoding": "chunked"},
                            }
                        ]
                    },
                )
                assert bad_hdr.status_code == 400
                assert bad_hdr.json()["detail"]["error_type"] == "validation_error"
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings
            app.dependency_overrides.pop(get_job_manager_dependency, None)

    def test_metrics_middleware_normalize_path_groups_routes(self) -> None:
        mw = MetricsMiddleware(app)
        assert mw._normalize_path("/metrics") == "/metrics"
        assert mw._normalize_path("/metrics/live") == "/metrics/live"
        assert mw._normalize_path("/metrics/unknown") == "/metrics"
        assert mw._normalize_path("/schedules") == "/schedules"
        assert mw._normalize_path("/schedules/1234") == "/schedules/{schedule_id}"
        assert mw._normalize_path("/schedules/1234/history") == "/schedules/{schedule_id}/history"
        assert mw._normalize_path("/docs/oauth2-redirect") == "/docs/oauth2-redirect"
        assert mw._normalize_path("/https://example.com/a/b") == "/download"

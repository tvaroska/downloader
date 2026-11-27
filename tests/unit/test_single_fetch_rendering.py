"""Unit tests for single-fetch Playwright rendering and ContentConfig wiring."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.config import (
    ContentConfig,
    Settings,
)
from downloader.content_converter import (
    BoundedCache,
    SelectorTimeoutError,
    _is_js_heavy_domain,
    should_use_playwright_for_html,
)
from downloader.dependencies import (
    get_http_client,
    get_pdf_semaphore,
    get_robots_checker,
)
from downloader.main import app
from downloader.pdf_generator import PDFGeneratorError


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
                    "downloader.browser.renderer.render_html_with_playwright",
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
                mock_render.assert_called_once()
                assert mock_render.call_args.args == ("https://example.com", "article")
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
                    "downloader.browser.renderer.render_html_with_playwright",
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
                    "downloader.browser.renderer.render_html_with_playwright",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("Chromium crashed"),
                ) as mock_render,
                patch(
                    "downloader.services.content_processor.should_use_playwright_for_html"
                ) as mock_auto_html,
                patch(
                    "downloader.browser.renderer.convert_content_with_playwright_fallback",
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
                    "downloader.browser.renderer.render_html_with_playwright",
                    new_callable=AsyncMock,
                    return_value=rendered_dom,
                ) as mock_render,
                patch(
                    "downloader.pdf_generator.generate_pdf_from_url",
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
                mock_render.assert_called_once()
                assert mock_render.call_args.args == ("https://example.com", None)
                mock_pdf.assert_called_once()
                assert mock_pdf.call_args.args == ("https://example.com",)
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
                    "downloader.pdf_generator.generate_pdf_from_url",
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
                    "downloader.services.content_processor.should_use_playwright_for_html",
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
        with patch("downloader.content_converter.get_settings", return_value=disabled_settings):
            html = b"<html><body><div id='root'>please enable javascript</div></body></html>"
            assert (
                should_use_playwright_for_html("https://medium.com/x", html, "text/html") is False
            )

        fake_now = 100.0
        monkeypatch.setattr("downloader.content_converter.time.monotonic", lambda: fake_now)
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

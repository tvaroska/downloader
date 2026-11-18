"""API tests for GET /{url} SSRF pre-flight rejection before robots/fetch/render (R18)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from downloader.dependencies import (
    get_browser_renderer,
    get_domain_throttle,
    get_http_client,
    get_robots_checker,
)
from downloader.main import app


@pytest.mark.unit
class TestDownloadEndpoint:
    """SSRF pre-flight pinning on GET /{url}."""

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://127.0.0.1/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/internal",
            "http://192.168.1.1/router",
        ],
    )
    def test_private_address_rejected_before_robots_and_fetch(
        self, api_client, blocked_url: str
    ) -> None:
        mock_http = MagicMock()
        mock_http.download = AsyncMock()
        mock_robots = MagicMock()
        mock_robots.check_allowed = AsyncMock()
        mock_throttle = MagicMock()
        mock_throttle.wait_if_needed = AsyncMock()
        mock_renderer = MagicMock()
        mock_renderer.render_html = AsyncMock()
        mock_renderer.render_pdf = AsyncMock()
        mock_renderer.render_screenshot = AsyncMock()

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
        app.dependency_overrides[get_browser_renderer] = lambda: mock_renderer
        try:
            resp = api_client.get(f"/{blocked_url}")
            assert resp.status_code == 400
            assert resp.json()["detail"]["error_type"] == "validation_error"
            mock_robots.check_allowed.assert_not_awaited()
            mock_throttle.wait_if_needed.assert_not_awaited()
            mock_http.download.assert_not_awaited()
            mock_renderer.render_html.assert_not_awaited()
            mock_renderer.render_pdf.assert_not_awaited()
            mock_renderer.render_screenshot.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_browser_renderer, None)

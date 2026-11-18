"""Unit tests for POST /render SSRF pre-flight rejection (R18)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from downloader.dependencies import (
    get_browser_renderer,
    get_domain_throttle,
    get_robots_checker,
)
from downloader.main import app


@pytest.mark.unit
class TestScreenshotAndRenderEndpoints:
    """SSRF pre-flight pinning on POST /render."""

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://127.0.0.1/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
            "http://[::1]/secret",
        ],
    )
    def test_post_render_rejects_private_and_metadata_urls(self, blocked_url: str) -> None:
        mock_robots = MagicMock()
        mock_robots.check_allowed = AsyncMock()
        mock_throttle = MagicMock()
        mock_throttle.wait_if_needed = AsyncMock()
        mock_renderer = MagicMock()
        mock_renderer.render_html = AsyncMock()
        mock_renderer.render_pdf = AsyncMock()
        mock_renderer.render_screenshot = AsyncMock()

        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
        app.dependency_overrides[get_browser_renderer] = lambda: mock_renderer
        try:
            with TestClient(app) as client:
                resp = client.post("/render", json={"url": blocked_url})
            assert resp.status_code == 400
            assert resp.json()["detail"]["error_type"] == "validation_error"
            mock_robots.check_allowed.assert_not_awaited()
            mock_throttle.wait_if_needed.assert_not_awaited()
            mock_renderer.render_html.assert_not_awaited()
            mock_renderer.render_pdf.assert_not_awaited()
            mock_renderer.render_screenshot.assert_not_awaited()
        finally:
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)
            app.dependency_overrides.pop(get_browser_renderer, None)

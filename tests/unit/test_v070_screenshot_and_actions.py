"""Offline unit & API tests for v0.7.0 screenshot/PDF toggle and browser interaction scripting."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.downloader.browser.actions import (
    MAX_BROWSER_ACTIONS,
    BrowserAction,
    BrowserActionError,
    execute_browser_actions,
    parse_actions_param,
)
from src.downloader.content_converter import SelectorTimeoutError
from src.downloader.crawling import RobotsDisallowedError
from src.downloader.dependencies import get_domain_throttle, get_http_client, get_robots_checker
from src.downloader.main import app
from src.downloader.models.responses import BatchURLRequest
from src.downloader.services.batch_processor import process_single_url_in_batch


class _FakeLocator:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    @property
    def first(self) -> _FakeLocator:
        return self

    async def scroll_into_view_if_needed(self, *, timeout: int = 5000) -> None:
        self._log.append(("scroll_into_view", timeout))


class _FakeKeyboard:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    async def press(self, key: str) -> None:
        self._log.append(("keyboard.press", key))


class _FakeMouse:
    def __init__(self, log: list[tuple[str, object]]) -> None:
        self._log = log

    async def wheel(self, x: int, y: int) -> None:
        self._log.append(("mouse.wheel", (x, y)))


class _FakePage:
    def __init__(self) -> None:
        self.log: list[tuple[str, object]] = []
        self.keyboard = _FakeKeyboard(self.log)
        self.mouse = _FakeMouse(self.log)

    async def click(self, selector: str, *, timeout: int = 5000) -> None:
        self.log.append(("click", selector))

    async def fill(self, selector: str, value: str, *, timeout: int = 5000) -> None:
        self.log.append(("fill", (selector, value)))

    async def select_option(self, selector: str, value: str, *, timeout: int = 5000) -> None:
        self.log.append(("select", (selector, value)))

    async def press(self, selector: str, key: str, *, timeout: int = 5000) -> None:
        self.log.append(("press", (selector, key)))

    async def hover(self, selector: str, *, timeout: int = 5000) -> None:
        self.log.append(("hover", selector))

    def locator(self, selector: str) -> _FakeLocator:
        self.log.append(("locator", selector))
        return _FakeLocator(self.log)

    async def wait_for_selector(
        self, selector: str, *, timeout: int = 5000, state: str = "visible"
    ) -> None:
        self.log.append(("wait_for_selector", (selector, state)))

    async def wait_for_timeout(self, duration_ms: int) -> None:
        self.log.append(("wait", duration_ms))


class TestBrowserActionValidationAndExecution:
    def test_parse_actions_param_empty(self) -> None:
        assert parse_actions_param(None) == []
        assert parse_actions_param("   ") == []

    def test_parse_actions_param_rejects_raw_js_eval(self) -> None:
        with pytest.raises(ValueError, match="Invalid browser action"):
            parse_actions_param(json.dumps([{"type": "evaluate", "script": "alert(1)"}]))

    def test_parse_actions_param_rejects_missing_required_fields(self) -> None:
        with pytest.raises(ValueError, match="requires 'selector'"):
            parse_actions_param(json.dumps([{"type": "click"}]))
        with pytest.raises(ValueError, match="requires 'value'"):
            parse_actions_param(json.dumps([{"type": "fill", "selector": "#email"}]))
        with pytest.raises(ValueError, match="requires 'key'"):
            parse_actions_param(json.dumps([{"type": "press"}]))

    def test_parse_actions_param_rejects_non_list_and_over_limit(self) -> None:
        with pytest.raises(ValueError, match="JSON array"):
            parse_actions_param('{"type": "wait"}')
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_actions_param("{bad json")
        too_many = [{"type": "wait", "duration_ms": 10} for _ in range(MAX_BROWSER_ACTIONS + 1)]
        with pytest.raises(ValueError, match="Too many browser actions"):
            parse_actions_param(json.dumps(too_many))

    async def test_execute_all_allowlisted_actions(self) -> None:
        page = _FakePage()
        actions = parse_actions_param(
            json.dumps(
                [
                    {"type": "click", "selector": "#accept"},
                    {"type": "fill", "selector": "#q", "value": "python"},
                    {"type": "select", "selector": "#country", "value": "US"},
                    {"type": "press", "selector": "#q", "key": "Enter"},
                    {"type": "press", "key": "Escape"},
                    {"type": "hover", "selector": ".menu"},
                    {"type": "scroll", "selector": "#footer"},
                    {"type": "scroll", "x": 0, "y": 500},
                    {"type": "wait_for_selector", "selector": ".results"},
                    {"type": "wait", "duration_ms": 50},
                ]
            )
        )
        executed = await execute_browser_actions(page, actions)
        assert executed == 10
        assert [entry[0] for entry in page.log] == [
            "click",
            "fill",
            "select",
            "press",
            "keyboard.press",
            "hover",
            "locator",
            "scroll_into_view",
            "mouse.wheel",
            "wait_for_selector",
            "wait",
        ]

    async def test_execute_browser_actions_wraps_timeout(self) -> None:
        page = _FakePage()
        page.click = AsyncMock(side_effect=TimeoutError("element never appeared"))  # type: ignore[method-assign]
        with pytest.raises(BrowserActionError, match="timed out"):
            await execute_browser_actions(
                page, [BrowserAction(type="click", selector="#missing", timeout_ms=200)]
            )


class TestScreenshotAndRenderEndpoints:
    def _setup_mock_dependencies(self):
        mock_robots = MagicMock()
        mock_robots.check_allowed = AsyncMock(return_value=None)
        mock_throttle = MagicMock()
        mock_throttle.wait_if_needed = AsyncMock(return_value=None)
        mock_http = MagicMock()
        mock_http.download = AsyncMock(
            return_value=(
                b"<html><body><h1>Fallback</h1></body></html>",
                {
                    "status_code": 200,
                    "headers": {"content-type": "text/html"},
                    "url": "https://example.com",
                    "size": 43,
                    "content_type": "text/html",
                    "http_version": "HTTP/1.1",
                    "connection_reused": False,
                },
            )
        )
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
        app.dependency_overrides[get_http_client] = lambda: mock_http
        return mock_robots

    def teardown_method(self) -> None:
        app.dependency_overrides.clear()

    def test_download_output_screenshot_toggle_png_and_jpeg(self) -> None:
        self._setup_mock_dependencies()
        fake_png = b"\x89PNG\r\n\x1a\nFAKEPNG"
        fake_jpeg = b"\xff\xd8\xffFAKEJPG"

        with (
            patch(
                "src.downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_negotiation.generate_screenshot_from_url",
                new=AsyncMock(side_effect=[fake_png, fake_jpeg]),
            ) as mock_shot,
            TestClient(app) as client,
        ):
            r_png = client.get("/https://example.com?output=screenshot&full_page=true")
            assert r_png.status_code == 200
            assert r_png.headers["content-type"] == "image/png"
            assert r_png.content == fake_png

            r_jpg = client.get(
                "/https://example.com?output=screenshot&screenshot_format=jpeg",
            )
            assert r_jpg.status_code == 200
            assert r_jpg.headers["content-type"] == "image/jpeg"
            assert r_jpg.content == fake_jpeg

            assert mock_shot.await_count == 2
            first_opts = mock_shot.await_args_list[0].args[1]
            second_opts = mock_shot.await_args_list[1].args[1]
            assert first_opts == {"type": "png", "full_page": True}
            assert second_opts == {"type": "jpeg", "full_page": False}

    def test_download_accept_image_png_and_multi_format(self) -> None:
        self._setup_mock_dependencies()
        fake_png = b"\x89PNG\r\n\x1a\nSHOT"
        with (
            patch(
                "src.downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_negotiation.generate_screenshot_from_url",
                new=AsyncMock(return_value=fake_png),
            ),
            TestClient(app) as client,
        ):
            r_single = client.get("/https://example.com", headers={"Accept": "image/png"})
            assert r_single.status_code == 200
            assert r_single.content == fake_png

            r_multi = client.get(
                "/https://example.com",
                headers={"Accept": "text/markdown, image/png"},
            )
            assert r_multi.status_code == 200
            payload = r_multi.json()
            assert "text/markdown" in payload
            assert base64.b64decode(payload["image/png"]) == fake_png

    def test_download_output_pdf_toggle(self) -> None:
        self._setup_mock_dependencies()
        with (
            patch(
                "src.downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                new=AsyncMock(return_value=b"%PDF-1.7 test"),
            ),
            TestClient(app) as client,
        ):
            resp = client.get("/https://example.com?output=pdf")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/pdf"
            assert resp.content.startswith(b"%PDF-1.7")

    def test_download_query_actions_and_validation(self) -> None:
        self._setup_mock_dependencies()
        with (
            patch(
                "src.downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                new=AsyncMock(return_value=b"<html><body><h1>Clicked!</h1></body></html>"),
            ) as mock_render,
            TestClient(app) as client,
        ):
            actions_json = json.dumps([{"type": "click", "selector": "#load"}])
            resp = client.get(
                "/https://example.com",
                params={"actions": actions_json},
                headers={"Accept": "text/markdown"},
            )
            assert resp.status_code == 200
            assert "# Clicked!" in resp.text
            assert resp.headers["x-rendered-with-js"] == "true"
            _, kwargs = mock_render.await_args
            assert len(kwargs["actions"]) == 1
            assert kwargs["actions"][0].type == "click"

            bad_resp = client.get('/https://example.com?actions=[{"type":"evaluate"}]')
            assert bad_resp.status_code == 400
            assert bad_resp.json()["detail"]["error_type"] == "validation_error"

    def test_post_render_endpoint_all_outputs_and_errors(self) -> None:
        mock_robots = self._setup_mock_dependencies()
        with (
            patch(
                "src.downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                new=AsyncMock(return_value=b"<html><body><h1>App</h1><p>Done</p></body></html>"),
            ),
            patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                new=AsyncMock(return_value=b"%PDF-1.7 rendered"),
            ),
            patch(
                "src.downloader.services.content_negotiation.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\x89PNG\r\n\x1a\nRENDER"),
            ),
            TestClient(app) as client,
        ):
            r_md = client.post(
                "/render",
                json={
                    "url": "https://example.com",
                    "output": "markdown",
                    "wait_for": "#app",
                    "actions": [{"type": "click", "selector": "#open"}],
                },
            )
            assert r_md.status_code == 200
            assert "# App" in r_md.text

            r_shot = client.post(
                "/render",
                json={
                    "url": "https://example.com",
                    "output": "screenshot",
                    "full_page": True,
                },
            )
            assert r_shot.status_code == 200
            assert r_shot.headers["content-type"] == "image/png"

            r_pdf = client.post(
                "/render",
                json={"url": "https://example.com", "output": "pdf"},
            )
            assert r_pdf.status_code == 200
            assert r_pdf.headers["content-type"] == "application/pdf"

            mock_robots.check_allowed = AsyncMock(
                side_effect=RobotsDisallowedError("https://example.com", "bot")
            )
            r_robots = client.post("/render", json={"url": "https://example.com"})
            assert r_robots.status_code == 403

        # Verify selector timeout and browser action error mapping
        self._setup_mock_dependencies()
        with (
            patch(
                "src.downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                new=AsyncMock(side_effect=SelectorTimeoutError("#never", 5000)),
            ),
            TestClient(app) as client,
        ):
            r_timeout = client.post(
                "/render",
                json={"url": "https://example.com", "wait_for": "#never"},
            )
            assert r_timeout.status_code == 408

        with (
            patch(
                "src.downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                new=AsyncMock(side_effect=BrowserActionError("click failed")),
            ),
            TestClient(app) as client,
        ):
            r_action_err = client.post(
                "/render",
                json={
                    "url": "https://example.com",
                    "actions": [{"type": "click", "selector": "#missing"}],
                },
            )
            assert r_action_err.status_code == 400
            assert r_action_err.json()["detail"]["error_type"] == "browser_action_error"

    async def test_batch_screenshot_and_actions(self) -> None:
        mock_http = MagicMock()
        pdf_sem = asyncio.Semaphore(2)
        req_shot = BatchURLRequest(
            url="https://example.com",
            format="screenshot",
            full_page=True,
            screenshot_format="jpeg",
        )
        req_actions = BatchURLRequest(
            url="https://example.com",
            format="markdown",
            actions=[BrowserAction(type="click", selector="#show")],
        )
        with (
            patch(
                "src.downloader.routes.batch.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "src.downloader.services.batch_processor.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\xff\xd8\xffJPEG"),
            ),
            patch(
                "src.downloader.services.batch_processor.render_html_with_playwright",
                new=AsyncMock(return_value=b"<html><body><h2>Expanded</h2></body></html>"),
            ),
        ):
            res_shot = await process_single_url_in_batch(
                req_shot, "text", 10, "REQ-1", mock_http, pdf_sem
            )
            assert res_shot.success is True
            assert res_shot.content_type == "image/jpeg"
            assert base64.b64decode(res_shot.content_base64 or "") == b"\xff\xd8\xffJPEG"

            res_md = await process_single_url_in_batch(
                req_actions, "text", 10, "REQ-2", mock_http, pdf_sem
            )
            assert res_md.success is True
            assert "## Expanded" in (res_md.content or "")

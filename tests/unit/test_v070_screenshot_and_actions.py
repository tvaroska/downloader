"""Offline unit & API tests for v0.7.0 screenshot/PDF toggle and browser interaction scripting."""

from __future__ import annotations

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.browser.actions import (
    MAX_BROWSER_ACTIONS,
    BrowserAction,
    BrowserActionError,
    execute_browser_actions,
    parse_actions_param,
)
from downloader.content_converter import SelectorTimeoutError
from downloader.crawling import RobotsDisallowedError
from downloader.dependencies import get_domain_throttle, get_http_client, get_robots_checker
from downloader.main import app
from downloader.models.responses import BatchURLRequest
from downloader.services.batch_processor import process_single_url_in_batch
from tests.fixtures.playwright_fakes import FakePage


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
        page = FakePage()
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
        assert [entry[0] for entry in page.calls] == [
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

    async def test_execute_browser_actions_wraps_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        page = FakePage()
        monkeypatch.setattr(
            page, "click", AsyncMock(side_effect=TimeoutError("element never appeared"))
        )
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
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)
        app.dependency_overrides.pop(get_http_client, None)

    def test_download_output_screenshot_toggle_png_and_jpeg(self) -> None:
        self._setup_mock_dependencies()
        fake_png = b"\x89PNG\r\n\x1a\nFAKEPNG"
        fake_jpeg = b"\xff\xd8\xffFAKEJPG"

        with (
            patch(
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
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
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
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
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.pdf_generator.generate_pdf_from_url",
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
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
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
            assert mock_render.await_args is not None
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
                "downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
                new=AsyncMock(return_value=b"<html><body><h1>App</h1><p>Done</p></body></html>"),
            ),
            patch(
                "downloader.pdf_generator.generate_pdf_from_url",
                new=AsyncMock(return_value=b"%PDF-1.7 rendered"),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
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
                "downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
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
                "downloader.routes.render.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
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
                "downloader.services.batch_processor.validate_url_async",
                new=AsyncMock(return_value="https://example.com"),
            ),
            patch(
                "downloader.pdf_generator.generate_screenshot_from_url",
                new=AsyncMock(return_value=b"\xff\xd8\xffJPEG"),
            ),
            patch(
                "downloader.browser.renderer.render_html_with_playwright",
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


@pytest.mark.unit
async def test_pdf_and_screenshot_action_errors_map_to_400_and_408(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R27: Failed actions and selector waits on PDF/screenshot map to 400 and 408, never 500."""
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    from downloader.errors import classify_domain_error
    from downloader.pdf_generator import PlaywrightPDFGenerator

    def _make_gen_with_page(page: AsyncMock) -> PlaywrightPDFGenerator:
        context = AsyncMock()
        context.new_page = AsyncMock(return_value=page)
        context.route = AsyncMock()
        context.on = MagicMock()
        context.close = AsyncMock()

        pool = MagicMock()
        pool.get_browser = AsyncMock(return_value=MagicMock())
        pool.create_context = AsyncMock(return_value=context)
        pool.release_browser = AsyncMock()

        gen = PlaywrightPDFGenerator(pool_size=1, idle_timeout=0.0)
        gen.pool = pool
        monkeypatch.setattr(gen, "ensure_started", AsyncMock(return_value=pool))
        return gen

    # 1. Failed action on PDF & screenshot -> BrowserActionError -> 400
    action_page = AsyncMock()
    action_page.goto = AsyncMock(return_value=AsyncMock(status=200))
    action_page.wait_for_load_state = AsyncMock()
    action_page.click = AsyncMock(side_effect=PlaywrightError("element detached"))
    gen_action = _make_gen_with_page(action_page)

    actions = [BrowserAction(type="click", selector="#missing")]
    with pytest.raises(BrowserActionError) as pdf_action_exc:
        await gen_action.generate_pdf("https://example.com", actions=actions)
    assert classify_domain_error(pdf_action_exc.value).status_code == 400

    with pytest.raises(BrowserActionError) as shot_action_exc:
        await gen_action.generate_screenshot("https://example.com", actions=actions)
    assert classify_domain_error(shot_action_exc.value).status_code == 400

    # 2. Selector timeout on PDF & screenshot -> SelectorTimeoutError -> 408
    selector_page = AsyncMock()
    selector_page.goto = AsyncMock(return_value=AsyncMock(status=200))
    selector_page.wait_for_load_state = AsyncMock()
    selector_page.wait_for_selector = AsyncMock(side_effect=PlaywrightTimeoutError("timed out"))
    gen_selector = _make_gen_with_page(selector_page)

    with pytest.raises(SelectorTimeoutError) as pdf_sel_exc:
        await gen_selector.generate_pdf("https://example.com", wait_for_selector="#never")
    assert classify_domain_error(pdf_sel_exc.value).status_code == 408

    with pytest.raises(SelectorTimeoutError) as shot_sel_exc:
        await gen_selector.generate_screenshot("https://example.com", wait_for_selector="#never")
    assert classify_domain_error(shot_sel_exc.value).status_code == 408

"""Unit tests for live production findings F-2026-11-16-002, F-2026-11-17-002, and F-2026-11-18-001."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from downloader import content_converter
from downloader.content_converter import (
    convert_content,
    should_use_playwright_for_html,
)
from downloader.dependencies import get_domain_throttle, get_http_client, get_robots_checker
from downloader.http_client import HTTPClient, HTTPClientError, UnsupportedMediaTypeError
from downloader.main import app
from downloader.models.responses import BatchURLRequest, ResponseMetadata
from downloader.ports import LocalBrowserRenderer
from downloader.services.batch_processor import process_single_url_in_batch
from downloader.services.content_processor import ConversionDeps, ConversionRequest, convert


class _CountingBinaryStream(httpx.AsyncByteStream):
    def __init__(self, chunk_count: int = 8) -> None:
        self._chunk_count = chunk_count
        self.chunks_read = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunk_count):
            self.chunks_read += 1
            yield b"\xff\xfb\x90\x64" * 4096


@pytest.mark.unit
class TestBinaryContentTypeGuardF20261116002:
    @pytest.mark.asyncio
    async def test_http_client_rejects_audio_mpeg_before_buffering_stream(self) -> None:
        stream = _CountingBinaryStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "audio/mpeg"},
                stream=stream,
                request=request,
            )

        client = HTTPClient(max_download_size=50 * 1024 * 1024)
        await client.close()
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [client._on_response_hook]},
        )
        try:
            with pytest.raises(UnsupportedMediaTypeError):
                await client.download(
                    "https://example.com/episode.mp3",
                    require_text_convertible=True,
                    allow_ocr_images=True,
                    target_format="html,markdown",
                )
            assert stream.chunks_read == 0
        finally:
            await client.close()

    @pytest.mark.parametrize(
        ("accept_header", "binary_ct"),
        [
            ("text/plain", "audio/mpeg"),
            ("text/markdown", "video/mp4"),
            ("text/html", "application/octet-stream"),
            ("text/html, text/markdown", "audio/mpeg"),
            ("text/plain", "application/zip"),
        ],
    )
    def test_download_route_returns_415_for_binary_media_types(
        self,
        accept_header: str,
        binary_ct: str,
    ) -> None:
        stream = _CountingBinaryStream()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": binary_ct},
                stream=stream,
                request=request,
            )

        real_http = HTTPClient()
        real_http._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
            event_hooks={"response": [real_http._on_response_hook]},
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None
        mock_throttle = AsyncMock()

        app.dependency_overrides[get_http_client] = lambda: real_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
        try:
            with TestClient(app) as client:
                resp = client.get(
                    "/https://example.com/media.bin",
                    headers={"Accept": accept_header},
                )
            assert resp.status_code == 415
            assert resp.json()["detail"]["error_type"] == "unsupported_media_type"
            assert stream.chunks_read == 0
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)
            app.dependency_overrides.pop(get_domain_throttle, None)

    @pytest.mark.asyncio
    async def test_convert_content_and_batch_reject_binary_payloads(self) -> None:
        with pytest.raises(UnsupportedMediaTypeError):
            convert_content(b"\x00\x01\x02\x03", "application/octet-stream", "text")
        with pytest.raises(UnsupportedMediaTypeError):
            convert_content(b"ID3\x04\x00", "audio/mpeg", "markdown")

        mock_http = AsyncMock()
        mock_http.download.return_value = (
            b"ID3\x04\x00binary",
            {
                "status_code": 200,
                "headers": {"content-type": "audio/mpeg"},
                "url": "https://example.com/ep.mp3",
                "size": 11,
                "content_type": "audio/mpeg",
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None
        mock_throttle = AsyncMock()

        batch_res = await process_single_url_in_batch(
            BatchURLRequest(url="https://example.com/ep.mp3", format="html"),
            default_format="html",
            timeout=10,
            request_id="REQ-415",
            http_client=mock_http,
            pdf_semaphore=asyncio.Semaphore(1),
            robots_checker=mock_robots,
            domain_throttle=mock_throttle,
        )
        assert batch_res.success is False
        assert batch_res.status_code == 415
        assert batch_res.error_type == "unsupported_media_type"


@pytest.mark.unit
class TestAutoRenderHeuristicsAndLogLevelsF20261117002:
    def test_ssr_articles_with_noscript_enable_json_or_missing_og_skip_browser(self) -> None:
        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()

        prose = (
            "Inference engineering for software engineers requires careful batching, "
            "KV-cache sizing, and you can enable JSON mode for structured outputs. "
        ) * 8  # > 500 chars of visible server-rendered article text

        # 1. Substack-like custom domain SSR article with <noscript> JS warning + "enable JSON"
        ssr_with_noscript = (
            "<!DOCTYPE html><html><head><title>Post</title></head><body>"
            "<noscript>This site requires JavaScript to run correctly. "
            "Please enable JavaScript.</noscript>"
            f"<article><p>{prose}</p></article></body></html>"
        ).encode()
        assert (
            should_use_playwright_for_html(
                "https://example.com/p/inference-engineering",
                ssr_with_noscript,
                "text/html; charset=utf-8",
            )
            is False
        )

        # 2. Short page (<500 chars) with metadata and "enable JSON mode" prose
        short_enable_json = (
            b"<!DOCTYPE html><html><head>"
            b"<meta property='og:title' content='JSON Guide'>"
            b"<meta property='og:description' content='Structured output tips'>"
            b"</head><body><p>How to enable JSON mode in your API client.</p></body></html>"
        )
        assert (
            should_use_playwright_for_html(
                "https://example.com/json-guide",
                short_enable_json,
                "text/html; charset=utf-8",
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_degraded_auto_render_emits_single_warning_and_zero_errors(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        content_converter._js_heavy_cache.clear()
        content_converter._static_html_cache.clear()
        spa_html = b"<html><head><title>App</title></head><body><div id='root'></div></body></html>"
        metadata: ResponseMetadata = {
            "status_code": 200,
            "headers": {"content-type": "text/html; charset=utf-8"},
            "url": "https://example.com/spa-timeout",
            "size": len(spa_html),
            "content_type": "text/html; charset=utf-8",
            "http_version": "HTTP/1.1",
            "connection_reused": False,
        }

        caplog.set_level(logging.WARNING)
        with patch(
            "downloader.content_converter.render_html_with_playwright",
            new=AsyncMock(side_effect=RuntimeError("Page.goto: Timeout 10000ms exceeded")),
        ):
            resp = await convert(
                ConversionRequest(
                    url="https://example.com/spa-timeout",
                    output_format="html",
                    content=spa_html,
                    metadata=metadata,
                ),
                ConversionDeps(renderer=LocalBrowserRenderer(asyncio.Semaphore(1))),
            )

        assert resp.headers["X-Rendered-With-JS"] == "false"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(errors) == 0
        assert len(warnings) == 1


@pytest.mark.unit
def test_upstream_4xx_logs_warning_while_5xx_logs_error_f20261118001(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_http = AsyncMock()
    mock_robots = AsyncMock()
    mock_robots.check_allowed.return_value = None
    mock_throttle = AsyncMock()

    app.dependency_overrides[get_http_client] = lambda: mock_http
    app.dependency_overrides[get_robots_checker] = lambda: mock_robots
    app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
    try:
        with TestClient(app) as client:
            caplog.set_level(logging.WARNING, logger="downloader.routes.download")

            mock_http.download.side_effect = HTTPClientError("HTTP 404: Not Found", status_code=404)
            r404 = client.get("/https://example.com/missing")
            assert r404.status_code == 404
            dl_records_404 = [r for r in caplog.records if r.name.startswith("downloader")]
            assert [r for r in dl_records_404 if r.levelno >= logging.ERROR] == []
            assert len([r for r in dl_records_404 if r.levelno == logging.WARNING]) == 1

            caplog.clear()
            mock_http.download.side_effect = HTTPClientError(
                "HTTP 503: Service Unavailable", status_code=503
            )
            r503 = client.get("/https://example.com/down")
            assert r503.status_code == 503
            dl_records_503 = [r for r in caplog.records if r.name.startswith("downloader")]
            assert len([r for r in dl_records_503 if r.levelno >= logging.ERROR]) == 1
    finally:
        app.dependency_overrides.pop(get_http_client, None)
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)

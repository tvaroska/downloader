"""Tests for the download endpoint."""

import base64
from unittest.mock import AsyncMock, patch

import pytest

from src.downloader.dependencies import get_http_client, get_pdf_semaphore
from src.downloader.http_client import HTTPClientError, HTTPTimeoutError
from src.downloader.main import app
from src.downloader.pdf_generator import PDFGeneratorError


class TestDownloadEndpoint:
    @pytest.mark.parametrize(
        "accept_header, mock_content, expected_content_type, expected_in_response",
        [
            (
                "application/json",
                b"<html>test</html>",
                "application/json",
                b"<html>test</html>",
            ),
            (
                "text/plain",
                b"<html><h1>Hello</h1><p>World</p></html>",
                "text/plain; charset=utf-8",
                "Hello World",
            ),
            (
                "text/markdown",
                b"<html><h1>Hello</h1><p>World</p><a href='https://test.com'>Link</a></html>",
                "text/markdown; charset=utf-8",
                "# Hello",
            ),
            (
                "text/html",
                b"<html><h1>Hello</h1></html>",
                "text/html; charset=utf-8",
                b"<html><h1>Hello</h1></html>",
            ),
        ],
    )
    def test_download_formats(
        self,
        api_client,
        accept_header,
        mock_content,
        expected_content_type,
        expected_in_response,
    ):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": len(mock_content),
                "headers": {"content-type": "text/html"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com", headers={"Accept": accept_header})
            assert response.status_code == 200
            assert response.headers["content-type"] == expected_content_type

            if accept_header == "application/json":
                data = response.json()
                assert data["success"] is True
                decoded_content = base64.b64decode(data["content"])
                assert decoded_content == expected_in_response
            elif isinstance(expected_in_response, bytes):
                assert response.content == expected_in_response
            else:
                assert expected_in_response in response.text
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_raw_format(self, api_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"binary data",
            {
                "url": "https://example.com/file.bin",
                "status_code": 200,
                "content_type": "application/octet-stream",
                "size": 11,
                "headers": {"content-type": "application/octet-stream"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com/file.bin")
            assert response.status_code == 200
            assert response.content == b"binary data"
            assert response.headers["content-type"] == "application/octet-stream"
            assert response.headers["X-Original-URL"] == "https://example.com/file.bin"
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_invalid_url(self, api_client):
        response = api_client.get("/invalid_url!")
        assert response.status_code == 400
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == "validation_error"

    @pytest.mark.parametrize(
        "error, expected_status, expected_error_type",
        [
            (HTTPTimeoutError("Request timed out"), 408, "timeout_error"),
            (
                HTTPClientError("HTTP 404: Not Found", status_code=404),
                404,
                "http_error",
            ),
        ],
    )
    def test_download_client_errors(self, api_client, error, expected_status, expected_error_type):
        mock_client = AsyncMock()
        mock_client.download.side_effect = error

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com")
            assert response.status_code == expected_status
            data = response.json()["detail"]
            assert data["success"] is False
            assert data["error_type"] == expected_error_type
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_pdf_format(self, api_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )

        pdf_content = b"%PDF-1.4 fake pdf content"

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                return_value=pdf_content,
            ) as mock_generate_pdf:
                response = api_client.get(
                    "/https://example.com", headers={"Accept": "application/pdf"}
                )
                assert response.status_code == 200
                assert response.headers["content-type"] == "application/pdf"
                assert "Content-Disposition" in response.headers
                assert "download.pdf" in response.headers["Content-Disposition"]
                assert response.content == pdf_content
                mock_generate_pdf.assert_called_once_with("https://example.com")
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_pdf_generation_error(self, api_client):
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.generate_pdf_from_url",
                side_effect=PDFGeneratorError("Browser failed to start"),
            ):
                response = api_client.get(
                    "/https://example.com", headers={"Accept": "application/pdf"}
                )
                assert response.status_code == 500
                data = response.json()["detail"]
                assert data["success"] is False
                assert data["error_type"] == "pdf_generation_error"
                assert "Browser failed to start" in data["error"]
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_download_pdf_service_unavailable(self, api_client):
        """Test PDF service unavailable when at capacity."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body>test content</body></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
            },
        )

        # Create a locked semaphore (0 permits = immediately locked)
        import asyncio

        locked_semaphore = asyncio.Semaphore(0)

        async def mock_get_http_client():
            return mock_client

        def mock_get_pdf_semaphore():
            return locked_semaphore

        app.dependency_overrides[get_http_client] = mock_get_http_client
        app.dependency_overrides[get_pdf_semaphore] = mock_get_pdf_semaphore
        try:
            response = api_client.get("/https://example.com", headers={"Accept": "application/pdf"})

            assert response.status_code == 503
            data = response.json()["detail"]
            assert data["success"] is False
            assert "temporarily unavailable" in data["error"]
            assert data["error_type"] == "service_unavailable"
        finally:
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_pdf_semaphore, None)

    # --- Authentication Tests ---

    def test_download_no_auth_required(self, api_client, env_no_auth, mock_http_client):
        """Test download works when no authentication is required."""

        async def mock_get_http_client():
            return mock_http_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com")
            assert response.status_code == 200
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    @pytest.mark.parametrize(
        "headers, expected_status, expected_error_type",
        [
            (None, 401, "authentication_required"),
            (
                {"Authorization": "Bearer wrong-key"},
                401,
                "authentication_failed",
            ),
            ({"X-API-Key": "wrong-key"}, 401, "authentication_failed"),
        ],
    )
    def test_download_auth_failed(
        self, api_client, env_with_auth, headers, expected_status, expected_error_type
    ):
        """Test download fails with missing or invalid API key."""
        response = api_client.get("/https://example.com", headers=headers)
        assert response.status_code == expected_status
        data = response.json()["detail"]
        assert data["success"] is False
        assert data["error_type"] == expected_error_type

    @pytest.mark.parametrize(
        "headers",
        [
            ({"Authorization": "Bearer test-key"}),
            ({"X-API-Key": "test-key"}),
        ],
    )
    def test_download_auth_valid(self, api_client, env_with_auth, mock_http_client, headers):
        """Test download works with valid API key."""

        async def mock_get_http_client():
            return mock_http_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com", headers=headers)
            assert response.status_code == 200
        finally:
            app.dependency_overrides.pop(get_http_client, None)


class TestRenderParameter:
    """Tests for the ?render=true query parameter."""

    def test_render_true_forces_playwright(self, api_client):
        """Test that ?render=true forces Playwright rendering."""
        mock_content = b"<html><body><h1>Static Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        rendered_content = b"<html><body><h1>Rendered Page</h1></body></html>"

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                return_value=rendered_content,
            ) as mock_render:
                response = api_client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 200
                assert response.headers["X-Rendered-With-JS"] == "true"
                assert response.content == rendered_content
                mock_render.assert_called_once_with("https://example.com", None)
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_render_false_uses_auto_detection(self, api_client):
        """Test that ?render=false (explicit) uses auto-detection."""
        mock_content = b"<html><body><h1>Static Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.should_use_playwright_for_html",
                return_value=False,
            ) as mock_detect:
                response = api_client.get(
                    "/https://example.com?render=false",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 200
                assert response.headers["X-Rendered-With-JS"] == "false"
                assert response.content == mock_content
                mock_detect.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_no_render_param_uses_auto_detection(self, api_client):
        """Test that missing render param uses auto-detection (default behavior)."""
        mock_content = b"<html><body><h1>Static Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.should_use_playwright_for_html",
                return_value=False,
            ) as mock_detect:
                response = api_client.get(
                    "/https://example.com",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 200
                assert response.headers["X-Rendered-With-JS"] == "false"
                mock_detect.assert_called_once()
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_render_true_graceful_degradation(self, api_client):
        """Test that ?render=true falls back to raw HTML on Playwright failure."""
        mock_content = b"<html><body><h1>Original</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                side_effect=Exception("Browser crash"),
            ):
                response = api_client.get(
                    "/https://example.com?render=true",
                    headers={"Accept": "text/html"},
                )
                # Should succeed with raw HTML on failure
                assert response.status_code == 200
                assert response.headers["X-Rendered-With-JS"] == "false"
                assert response.content == mock_content
        finally:
            app.dependency_overrides.pop(get_http_client, None)


class TestWaitForParameter:
    """Tests for the ?wait_for=<selector> query parameter."""

    def test_wait_for_implies_render(self, api_client):
        """Test that ?wait_for automatically enables rendering."""
        mock_content = b"<html><body><h1>Static Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        rendered_content = b"<html><body><h1>Rendered</h1></body></html>"

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                return_value=rendered_content,
            ) as mock_render:
                response = api_client.get(
                    "/https://example.com?wait_for=.content",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 200
                assert response.headers["X-Rendered-With-JS"] == "true"
                # Verify render was called with the selector
                mock_render.assert_called_once_with("https://example.com", ".content")
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_wait_for_selector_timeout(self, api_client):
        """Test that selector timeout returns 408."""
        from src.downloader.content_converter import SelectorTimeoutError

        mock_content = b"<html><body><h1>Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                side_effect=SelectorTimeoutError(".nonexistent", 10000),
            ):
                response = api_client.get(
                    "/https://example.com?wait_for=.nonexistent",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 408
                data = response.json()
                assert data["detail"]["error_type"] == "selector_timeout_error"
                assert ".nonexistent" in data["detail"]["error"]
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_wait_for_with_render_true(self, api_client):
        """Test ?wait_for=.foo&render=true works correctly."""
        mock_content = b"<html><body><h1>Page</h1></body></html>"
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            mock_content,
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html; charset=utf-8",
                "size": len(mock_content),
                "headers": {"content-type": "text/html; charset=utf-8"},
            },
        )

        rendered_content = b"<html><body><h1>Rendered</h1></body></html>"

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            with patch(
                "src.downloader.services.content_processor.render_html_with_playwright",
                return_value=rendered_content,
            ) as mock_render:
                response = api_client.get(
                    "/https://example.com?render=true&wait_for=article",
                    headers={"Accept": "text/html"},
                )
                assert response.status_code == 200
                mock_render.assert_called_once_with("https://example.com", "article")
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_wait_for_max_length(self, api_client):
        """Test that very long selectors are rejected (422)."""
        long_selector = "a" * 501  # Exceeds max_length=500

        response = api_client.get(
            f"/https://example.com?wait_for={long_selector}",
            headers={"Accept": "text/html"},
        )
        # FastAPI returns 422 for validation errors
        assert response.status_code == 422

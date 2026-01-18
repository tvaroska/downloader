"""Tests for multi-format support."""

from unittest.mock import AsyncMock

from src.downloader.dependencies import get_http_client
from src.downloader.main import app


class TestMultiFormat:
    """Test multi-format functionality."""

    def test_parse_accept_headers_single(self):
        """Test parsing single Accept header."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers("text/html")
        assert result == ["html"]

    def test_parse_accept_headers_comma_separated(self):
        """Test parsing comma-separated Accept header."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers("text/html, text/markdown, application/pdf")
        assert set(result) == {"html", "markdown", "pdf"}

    def test_parse_accept_headers_with_quality(self):
        """Test parsing Accept header with quality parameters."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers("text/html;q=0.9, text/markdown;q=1.0")
        assert set(result) == {"html", "markdown"}

    def test_parse_accept_headers_list(self):
        """Test parsing list of Accept headers."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers(["text/html", "text/markdown"])
        assert set(result) == {"html", "markdown"}

    def test_parse_accept_headers_empty(self):
        """Test parsing empty Accept header."""
        from src.downloader.services.content_processor import parse_accept_headers

        assert parse_accept_headers(None) == []
        assert parse_accept_headers("") == []
        assert parse_accept_headers([]) == []

    def test_parse_accept_headers_unsupported(self):
        """Test parsing with unsupported formats."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers("text/html, image/png, text/markdown")
        assert set(result) == {"html", "markdown"}
        assert "image/png" not in result

    def test_parse_accept_headers_duplicates(self):
        """Test parsing with duplicate formats."""
        from src.downloader.services.content_processor import parse_accept_headers

        result = parse_accept_headers("text/html, text/html, text/markdown")
        assert result == ["html", "markdown"]

    def test_format_to_mime_type(self):
        """Test format to MIME type conversion."""
        from src.downloader.services.content_processor import _format_to_mime_type

        assert _format_to_mime_type("html") == "text/html"
        assert _format_to_mime_type("markdown") == "text/markdown"
        assert _format_to_mime_type("pdf") == "application/pdf"
        assert _format_to_mime_type("text") == "text/plain"
        assert _format_to_mime_type("json") == "application/json"

    def test_multi_format_basic(self, api_client):
        """Test basic multi-format request with 2 formats."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><h1>Hello</h1><p>World</p></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get(
                "/https://example.com", headers={"Accept": "text/html, text/markdown"}
            )

            assert response.status_code == 200
            assert response.headers["content-type"] == "application/json"

            data = response.json()
            assert "text/html" in data
            assert "text/markdown" in data
            assert "Hello" in data["text/html"]
            assert "# Hello" in data["text/markdown"] or "Hello" in data["text/markdown"]
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_single_format_backward_compatible(self, api_client):
        """Test single format request still works (backward compatibility)."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><h1>Hello</h1><p>World</p></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 39,
                "headers": {"content-type": "text/html"},
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get("/https://example.com", headers={"Accept": "text/html"})

            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
            assert b"<html>" in response.content
            assert b"Hello" in response.content
        finally:
            app.dependency_overrides.pop(get_http_client, None)

    def test_multi_format_with_errors_key(self, api_client):
        """Test multi-format response includes errors dict when appropriate."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><h1>Hello</h1></html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 25,
                "headers": {"content-type": "text/html"},
                "http_version": "HTTP/1.1",
                "connection_reused": False,
            },
        )

        async def mock_get_http_client():
            return mock_client

        app.dependency_overrides[get_http_client] = mock_get_http_client
        try:
            response = api_client.get(
                "/https://example.com", headers={"Accept": "text/html, text/plain"}
            )

            assert response.status_code == 200
            data = response.json()

            # Should have both formats
            assert "text/html" in data
            assert "text/plain" in data
        finally:
            app.dependency_overrides.pop(get_http_client, None)

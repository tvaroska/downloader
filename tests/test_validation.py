import pytest
from fastapi import HTTPException

from src.downloader.utils.validation import validate_url, sanitize_url


class TestValidateUrl:
    def test_valid_https_url(self):
        url = "https://example.com"
        result = validate_url(url)
        assert result == url

    def test_valid_http_url(self):
        url = "http://example.com"
        result = validate_url(url)
        assert result == url

    def test_url_without_protocol(self):
        url = "example.com"
        result = validate_url(url)
        assert result == "https://example.com"

    def test_empty_url(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_url("")
        assert exc_info.value.status_code == 400
        assert "cannot be empty" in exc_info.value.detail

    def test_invalid_domain(self):
        with pytest.raises(HTTPException) as exc_info:
            validate_url("https://invalid_domain!")
        assert exc_info.value.status_code == 400
        assert "Invalid domain name" in exc_info.value.detail

    def test_url_with_path(self):
        url = "https://example.com/path/to/resource"
        result = validate_url(url)
        assert result == url

    def test_url_with_query_params(self):
        url = "https://example.com/search?q=test"
        result = validate_url(url)
        assert result == url


class TestSanitizeUrl:
    def test_url_with_whitespace(self):
        url = "  https://example.com  "
        result = sanitize_url(url)
        assert result == "https://example.com"

    def test_url_with_internal_spaces(self):
        url = "https://example .com"
        result = sanitize_url(url)
        assert result == "https://example.com"

    def test_clean_url(self):
        url = "https://example.com"
        result = sanitize_url(url)
        assert result == url
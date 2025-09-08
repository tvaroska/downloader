import pytest

from src.downloader.validation import (
    URLValidationError,
    sanitize_user_agent,
    validate_url,
)


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
        assert result == "http://example.com"

    def test_empty_url(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("")
        assert "must be a non-empty string" in str(exc_info.value)

    def test_none_url(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url(None)
        assert "must be a non-empty string" in str(exc_info.value)

    def test_invalid_hostname(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("https://invalid_domain!")
        assert "Invalid hostname format" in str(exc_info.value)

    def test_no_hostname(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("https://")
        assert "must have a valid hostname" in str(exc_info.value)

    def test_invalid_scheme(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("ftp://example.com")
        assert "must use http or https scheme" in str(exc_info.value)

    def test_localhost_blocked(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("http://localhost")
        assert "private addresses is not allowed" in str(exc_info.value)

    def test_private_ip_blocked(self):
        with pytest.raises(URLValidationError) as exc_info:
            validate_url("http://192.168.1.1")
        assert "private addresses is not allowed" in str(exc_info.value)

    def test_url_with_path(self):
        url = "https://example.com/path/to/resource"
        result = validate_url(url)
        assert result == url

    def test_url_with_query_params(self):
        url = "https://example.com/search?q=test"
        result = validate_url(url)
        assert result == url

    def test_url_with_whitespace(self):
        url = "  https://example.com  "
        result = validate_url(url)
        assert result == "https://example.com"


class TestSanitizeUserAgent:
    def test_none_user_agent(self):
        result = sanitize_user_agent(None)
        assert result.startswith("REST-API-Downloader/")

    def test_empty_user_agent(self):
        result = sanitize_user_agent("")
        assert result.startswith("REST-API-Downloader/")

    def test_custom_user_agent(self):
        ua = "MyBot/1.0"
        result = sanitize_user_agent(ua)
        assert result == ua

    def test_sanitize_harmful_chars(self):
        ua = "MyBot<script>alert('xss')</script>/1.0"
        result = sanitize_user_agent(ua)
        assert "<script>" not in result
        assert "MyBot" in result

    def test_length_limit(self):
        ua = "x" * 300
        result = sanitize_user_agent(ua)
        assert len(result) == 200

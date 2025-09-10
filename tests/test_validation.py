import pytest

from src.downloader.validation import (
    URLValidationError,
    sanitize_user_agent,
    validate_url,
)


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://example.com", "https://example.com"),
            ("http://example.com", "http://example.com"),
            ("example.com", "http://example.com"),
            ("https://example.com/path/to/resource", "https://example.com/path/to/resource"),
            ("https://example.com/search?q=test", "https://example.com/search?q=test"),
            ("  https://example.com  ", "https://example.com"),
        ],
    )
    def test_validate_url_valid(self, url, expected):
        """Test that valid URLs are handled correctly."""
        assert validate_url(url) == expected

    @pytest.mark.parametrize(
        "url, error_message",
        [
            ("", "must be a non-empty string"),
            (None, "must be a non-empty string"),
            ("https://invalid_domain!", "Invalid hostname format"),
            ("https://", "must have a valid hostname"),
            ("ftp://example.com", "must use http or https scheme"),
            ("http://localhost", "private addresses is not allowed"),
            ("http://192.168.1.1", "private addresses is not allowed"),
        ],
    )
    def test_validate_url_invalid(self, url, error_message):
        """Test that invalid URLs raise a URLValidationError."""
        with pytest.raises(URLValidationError, match=error_message):
            validate_url(url)


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
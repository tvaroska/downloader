"""Unit tests for URL ASCII canonicalization, rejection of unsafe chars, and origin_key (`R33`)."""

from __future__ import annotations

import pytest

from downloader.config import Settings, SSRFConfig
from downloader.validation import URLValidationError, origin_key, validate_url_async


@pytest.mark.unit
@pytest.mark.asyncio
async def test_returns_ascii_canonical_url() -> None:
    """Non-ASCII IDN hosts are punycoded and non-ASCII path/query/fragment are percent-encoded."""
    settings = Settings(ssrf=SSRFConfig(resolve_dns=False))
    raw = "https://münchen.de/café/путь?q=значение#якорь"
    canonical = await validate_url_async(raw, settings)
    assert canonical.isascii()
    assert canonical.startswith("https://xn--mnchen-3ya.de/caf%C3%A9/")
    assert "%D0%BF%D1%83%D1%82%D1%8C" in canonical
    assert "q=%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D0%B5" in canonical
    assert canonical.endswith("#%D1%8F%D0%BA%D0%BE%D1%80%D1%8C")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "https://example.com/foo\x00bar",
        "https://example.com/foo\nbar",
        "https://example.com/foo\rbar",
        "https://example.com/foo bar",
        "https://example.com/foo\\bar",
    ],
)
async def test_rejects_control_chars_whitespace_and_backslashes(bad_url: str) -> None:
    """Control characters, internal whitespace, and backslashes must raise URLValidationError."""
    settings = Settings(ssrf=SSRFConfig(resolve_dns=False))
    with pytest.raises(URLValidationError):
        await validate_url_async(bad_url, settings)


@pytest.mark.unit
def test_origin_key_strips_userinfo_and_trailing_dot() -> None:
    """origin_key returns canonical scheme://host[:port] without userinfo."""
    assert origin_key("https://user:secret@Example.COM.:443/path?a=1") == "https://example.com"
    assert origin_key("http://user@example.com:8080/path") == "http://example.com:8080"

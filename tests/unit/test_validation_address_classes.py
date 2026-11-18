"""Unit tests for non-global IP address SSRF blocking including RFC 6598 100.64.0.0/10 (`R5`)."""

from __future__ import annotations

import pytest

from downloader.config import Settings
from downloader.validation import SSRFProtectionError
from tests.conftest import validate_url_sync as validate_url


@pytest.mark.unit
@pytest.mark.parametrize(
    "blocked_url",
    [
        "http://100.64.0.1/internal",
        "http://100.127.255.254:8080/health",
        "http://[::ffff:100.64.0.1]/mapped",
        "http://198.18.0.1/benchmark",
        "http://192.0.0.1/ietf-protocol",
        "http://[2001:db8::1]/doc-prefix",
    ],
)
def test_shared_address_space_is_blocked(blocked_url: str) -> None:
    settings = Settings()
    with pytest.raises(SSRFProtectionError):
        validate_url(blocked_url, settings)


@pytest.mark.unit
def test_global_unicast_address_remains_allowed() -> None:
    settings = Settings()
    assert (
        validate_url("https://93.184.215.14/article", settings) == "https://93.184.215.14/article"
    )

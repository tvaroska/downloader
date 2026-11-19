"""Verification of the offline DNS and socket guard in `tests/conftest.py` (`R81`)."""

from __future__ import annotations

import errno
import socket
from typing import Any

import pytest

from tests.conftest import _is_allowed_target, _resolve_offline_host


@pytest.mark.smoke
def test_requires_redis_items_may_reach_configured_redis_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`requires_redis` items can resolve and connect to the configured `REDIS_URI` host only."""
    monkeypatch.setenv("REDIS_URI", "redis://downloader-ci-redis:6379/0")
    allowed: set[str] = set()

    def _fake_orig_getaddrinfo(
        host: Any,
        port: Any,
        *args: Any,
        **kwargs: Any,
    ) -> list[tuple[Any, ...]]:
        del args, kwargs
        assert host == "downloader-ci-redis"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.18.0.5", int(port or 6379)))]

    resolved = _resolve_offline_host(
        _fake_orig_getaddrinfo,
        "downloader-ci-redis",
        6379,
        allowed_redis_hosts=allowed,
        allow_configured_redis=True,
    )
    assert resolved[0][4][0] == "172.18.0.5"
    assert _is_allowed_target("downloader-ci-redis", allowed) is True
    assert _is_allowed_target("172.18.0.5", allowed) is True
    assert _is_allowed_target("unconfigured.example.net", allowed) is False

    # Unmarked items must still reject the Redis CI host.
    with pytest.raises(socket.gaierror):
        _resolve_offline_host(
            _fake_orig_getaddrinfo,
            "downloader-ci-redis",
            6379,
            allowed_redis_hosts=set(),
            allow_configured_redis=False,
        )

    # Live socket connection from a smoke test (not marked requires_redis) remains blocked.
    with pytest.raises(OSError) as exc_info:
        socket.create_connection(("172.18.0.5", 6379), timeout=0.1)
    assert exc_info.value.errno == errno.ENETUNREACH

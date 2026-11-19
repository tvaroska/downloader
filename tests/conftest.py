"""Root conftest - imports all fixture modules.

This file auto-discovers and loads all fixtures from the fixtures/ directory,
making them available to all tests throughout the test suite.
"""

import asyncio
import errno
import ipaddress
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import anyio
import pytest

from downloader.config import AuthConfig, Settings, reload_settings
from downloader.validation import validate_url_async

# Isolate test suite from developer workstation `.env` files while keeping
# explicit `AuthConfig(_env_file=...)` tests functional.
AuthConfig.model_config["env_file"] = None
Settings.model_config["env_file"] = None
reload_settings()


def validate_url_sync(url: Any, settings: Settings | None = None) -> str:
    """Synchronous test helper delegating directly to `validate_url_async`."""
    return asyncio.run(validate_url_async(url, settings))


# Import all fixture modules to make them available globally
pytest_plugins = [
    "tests.fixtures.api_fixtures",
    "tests.fixtures.mock_fixtures",
    "tests.fixtures.data_fixtures",
    "tests.fixtures.env_fixtures",
    "tests.fixtures.html_fixtures",
]

_OFFLINE_FIXTURE_HOSTS = frozenset(
    {
        "example.com",
        "www.example.com",
        "api.example.com",
        "example.org",
        "test.com",
        "success.com",
        "success1.com",
        "success2.com",
        "fail.com",
        "fail1.com",
        "fail2.com",
        "slow.com",
        "large.com",
        "notfound.com",
        "error.com",
        "google.com",
        "medium.com",
        "substack.com",
        "example.substack.com",
        "test.substack.com",
        "ontologist.substack.com",
        "substackcdn.com",
    }
)
_LOOPBACK_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_NUMBERED_EXAMPLE_RE = re.compile(r"example\d+\.com")


def _extract_redis_uri_host(redis_uri: str | None) -> str | None:
    if not redis_uri:
        return None
    try:
        parsed = urlparse(redis_uri)
    except ValueError:
        return None
    host = parsed.hostname
    return host.lower() if host else None


def _is_allowed_target(host: object, allowed_hosts: set[str]) -> bool:
    host_str = (
        host.decode("ascii", errors="ignore") if isinstance(host, bytes) else str(host or "")
    ).strip("[]")
    host_lower = host_str.lower()
    if host_lower in _LOOPBACK_HOSTS or host_lower in allowed_hosts:
        return True
    try:
        return ipaddress.ip_address(host_str).is_loopback
    except ValueError:
        return False


def _resolve_offline_host(
    orig_getaddrinfo: Any,
    host: Any,
    port: Any,
    *args: Any,
    allowed_redis_hosts: set[str],
    allow_configured_redis: bool,
    **kwargs: Any,
) -> Any:
    if host is None:
        return orig_getaddrinfo(host, port, *args, **kwargs)
    host_str = host.decode("ascii", errors="ignore") if isinstance(host, bytes) else str(host)
    try:
        ipaddress.ip_address(host_str.strip("[]"))
        return orig_getaddrinfo(host, port, *args, **kwargs)
    except ValueError:
        pass

    host_lower = host_str.lower()
    if allow_configured_redis:
        configured_host = _extract_redis_uri_host(os.getenv("REDIS_URI"))
        if configured_host and host_lower == configured_host:
            allowed_redis_hosts.add(configured_host)
            results = orig_getaddrinfo(host, port, *args, **kwargs)
            for item in results:
                sockaddr = item[4]
                if isinstance(sockaddr, tuple) and sockaddr:
                    allowed_redis_hosts.add(str(sockaddr[0]).lower())
            return results

    if host_lower in _LOOPBACK_HOSTS:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]
    if host_lower in _OFFLINE_FIXTURE_HOSTS or _NUMBERED_EXAMPLE_RE.fullmatch(host_lower):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port or 0))]
    raise socket.gaierror(socket.EAI_NONAME, f"Name or service not known: {host_str}")


def _assert_allowed_target(host: object, allowed_hosts: set[str], label: object) -> None:
    if not _is_allowed_target(host, allowed_hosts):
        raise OSError(
            errno.ENETUNREACH,
            f"Network is unreachable (offline fixture guard blocked {label})",
        )


@pytest.fixture(autouse=True)
def _offline_fixture_dns(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve allowlisted fixture domains deterministically without live network DNS.

    Skipped when a test carries `@pytest.mark.network` or `@pytest.mark.e2e`.
    For `@pytest.mark.requires_redis` tests, allows DNS resolution and TCP connections
    to the configured `REDIS_URI` host (e.g., `downloader-ci-redis` in Cloud Build)
    while keeping all other external hosts blocked.
    """
    if request.node.get_closest_marker("network") or request.node.get_closest_marker("e2e"):
        return

    # Refresh settings at test start so prior tests patching env never leak into global `_settings`.
    reload_settings()

    allow_configured_redis = bool(request.node.get_closest_marker("requires_redis"))
    allowed_redis_hosts: set[str] = set()

    def _current_allowed_hosts() -> set[str]:
        if allow_configured_redis:
            configured = _extract_redis_uri_host(os.getenv("REDIS_URI"))
            if configured:
                allowed_redis_hosts.add(configured)
            return allowed_redis_hosts
        return set()

    orig_getaddrinfo = socket.getaddrinfo
    orig_anyio_connect_tcp = anyio.connect_tcp
    orig_create_connection = socket.create_connection
    orig_loop_create_connection = asyncio.BaseEventLoop.create_connection

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: _resolve_offline_host(
            orig_getaddrinfo,
            host,
            port,
            *args,
            allowed_redis_hosts=allowed_redis_hosts,
            allow_configured_redis=allow_configured_redis,
            **kwargs,
        ),
    )

    async def _guarded_connect_tcp(remote_host: Any, remote_port: Any, *args: Any, **kwargs: Any):
        _assert_allowed_target(
            remote_host, _current_allowed_hosts(), f"{remote_host}:{remote_port}"
        )
        return await orig_anyio_connect_tcp(remote_host, remote_port, *args, **kwargs)

    def _guarded_create_connection(address: Any, *args: Any, **kwargs: Any):
        host = address[0] if isinstance(address, tuple) and address else ""
        _assert_allowed_target(host, _current_allowed_hosts(), address)
        return orig_create_connection(address, *args, **kwargs)

    async def _guarded_loop_create_connection(
        self: asyncio.BaseEventLoop,
        protocol_factory: Any,
        host: Any = None,
        port: Any = None,
        *args: Any,
        **kwargs: Any,
    ):
        if host is not None:
            _assert_allowed_target(host, _current_allowed_hosts(), f"{host}:{port}")
        return await orig_loop_create_connection(
            self, protocol_factory, host, port, *args, **kwargs
        )

    monkeypatch.setattr(anyio, "connect_tcp", _guarded_connect_tcp)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", _guarded_loop_create_connection)


def _apply_item_tier_and_timeout(rootpath: Any, item: Any) -> None:
    tier_markers = ("smoke", "unit", "integration", "e2e")
    rel_parts = item.path.relative_to(rootpath).parts
    needs_external = any(
        item.get_closest_marker(m) for m in ("requires_playwright", "requires_redis", "network")
    )
    if "integration" in rel_parts and needs_external and not item.get_closest_marker("integration"):
        item.add_marker(pytest.mark.integration)
    elif "e2e" in rel_parts and not item.get_closest_marker("e2e"):
        item.add_marker(pytest.mark.e2e)
    elif "smoke" in rel_parts and not item.get_closest_marker("smoke"):
        item.add_marker(pytest.mark.smoke)

    if not any(item.get_closest_marker(m) for m in tier_markers):
        item.add_marker(pytest.mark.unit)

    if not item.get_closest_marker("timeout"):
        if item.get_closest_marker("e2e"):
            item.add_marker(pytest.mark.timeout(120))
        elif item.get_closest_marker("integration"):
            item.add_marker(pytest.mark.timeout(60))
        elif item.get_closest_marker("network"):
            item.add_marker(pytest.mark.timeout(45))


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    """Ensure every test has a tier marker and apply tier-based timeouts."""
    for item in items:
        _apply_item_tier_and_timeout(config.rootpath, item)

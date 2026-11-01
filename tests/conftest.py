"""Root conftest - imports all fixture modules.

This file auto-discovers and loads all fixtures from the fixtures/ directory,
making them available to all tests throughout the test suite.
"""

import errno
import ipaddress
import re
import socket

import anyio
import pytest

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


@pytest.fixture(autouse=True)
def _offline_fixture_dns(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve allowlisted fixture domains deterministically without live network DNS.

    Skipped when a test carries `@pytest.mark.network` or `@pytest.mark.e2e`.
    Unknown domains raise `socket.gaierror` and `localhost` resolves to `127.0.0.1`
    so SSRF loopback and DNS-failure tests remain 100% real.
    """
    if request.node.get_closest_marker("network") or request.node.get_closest_marker("e2e"):
        return

    orig_getaddrinfo = socket.getaddrinfo

    def _getaddrinfo(host, port, *args, **kwargs):
        if host is None:
            return orig_getaddrinfo(host, port, *args, **kwargs)
        host_str = host.decode("ascii", errors="ignore") if isinstance(host, bytes) else str(host)
        try:
            ipaddress.ip_address(host_str.strip("[]"))
            return orig_getaddrinfo(host, port, *args, **kwargs)
        except ValueError:
            pass

        host_lower = host_str.lower()
        if host_lower in _LOOPBACK_HOSTS:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 0))]
        if host_lower in _OFFLINE_FIXTURE_HOSTS or _NUMBERED_EXAMPLE_RE.fullmatch(host_lower):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port or 0))]
        raise socket.gaierror(socket.EAI_NONAME, f"Name or service not known: {host_str}")

    monkeypatch.setattr(socket, "getaddrinfo", _getaddrinfo)

    orig_anyio_connect_tcp = anyio.connect_tcp
    orig_create_connection = socket.create_connection

    def _is_loopback_target(host: object) -> bool:
        host_str = (
            host.decode("ascii", errors="ignore") if isinstance(host, bytes) else str(host or "")
        ).strip("[]")
        if host_str.lower() in _LOOPBACK_HOSTS:
            return True
        try:
            return ipaddress.ip_address(host_str).is_loopback
        except ValueError:
            return False

    async def _guarded_connect_tcp(remote_host, remote_port, *args, **kwargs):
        if not _is_loopback_target(remote_host):
            raise OSError(
                errno.ENETUNREACH,
                f"Network is unreachable (offline fixture guard blocked {remote_host}:{remote_port})",
            )
        return await orig_anyio_connect_tcp(remote_host, remote_port, *args, **kwargs)

    def _guarded_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else ""
        if not _is_loopback_target(host):
            raise OSError(
                errno.ENETUNREACH,
                f"Network is unreachable (offline fixture guard blocked {address})",
            )
        return orig_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(anyio, "connect_tcp", _guarded_connect_tcp)
    monkeypatch.setattr(socket, "create_connection", _guarded_create_connection)


def pytest_collection_modifyitems(config, items):
    """Ensure every test has a tier marker and apply tier-based timeouts."""
    tier_markers = ("smoke", "unit", "integration", "e2e")
    for item in items:
        rel_parts = item.path.parts
        if "integration" in rel_parts and not item.get_closest_marker("integration"):
            item.add_marker(pytest.mark.integration)
        elif "e2e" in rel_parts and not item.get_closest_marker("e2e"):
            item.add_marker(pytest.mark.e2e)
        elif "smoke" in rel_parts and not item.get_closest_marker("smoke"):
            item.add_marker(pytest.mark.smoke)

        if not any(item.get_closest_marker(m) for m in tier_markers):
            item.add_marker(pytest.mark.unit)

        # E2E tests get longer timeout (120s)
        if item.get_closest_marker("e2e"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(120))
        # Integration tests get medium timeout (60s)
        elif item.get_closest_marker("integration"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(60))
        # Network tests get medium timeout (45s)
        elif item.get_closest_marker("network"):
            if not item.get_closest_marker("timeout"):
                item.add_marker(pytest.mark.timeout(45))

"""Unit tests for `/metrics` auth, production docs 404, trusted proxy rate limits, and SSRF."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

from downloader.auth import verify_api_key
from downloader.config import (
    AuthConfig,
    RateLimitConfig,
    Settings,
    reload_settings,
)
from downloader.dependencies import (
    get_http_client,
    get_robots_checker,
)
from downloader.http_client import HTTPClient, HTTPClientError
from downloader.main import app
from downloader.ssrf_transport import SSRFSafeNetworkBackend
from downloader.validation import (
    SSRFProtectionError,
    URLValidationError,
)
from tests.conftest import validate_url_sync as validate_url


@pytest.mark.unit
class TestTrackBAuthDocsRateLimitAndSSRF:
    """Track B: .env auth parity, prod OpenAPI disablement, XFF rate limits, and DNS-pinned SSRF."""

    def test_auth_config_loads_dotenv_file_and_constant_time_verify(self, tmp_path: Path) -> None:
        dotenv_path = tmp_path / ".env"
        dotenv_path.write_text("DOWNLOADER_KEY=dotenv-secret-key\n", encoding="utf-8")

        with patch.dict("os.environ", {}, clear=True):
            cfg = AuthConfig(_env_file=str(dotenv_path))
            assert cfg.api_key == "dotenv-secret-key"

            settings = Settings(auth=cfg)
            with patch("downloader.auth.get_settings", return_value=settings):
                assert verify_api_key("dotenv-secret-key") is True
                assert verify_api_key("wrong-key") is False

    @pytest.mark.parametrize(
        "doc_path",
        ["/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"],
    )
    def test_docs_disabled_in_production_and_enabled_in_development(self, doc_path: str) -> None:
        prev_settings = getattr(app.state, "settings", None)
        try:
            with TestClient(app) as client:
                app.state.settings = Settings(
                    environment="production",
                    auth=AuthConfig(api_key="test-prod-key"),
                )
                prod_resp = client.get(doc_path)
                assert prod_resp.status_code == 404
                assert client.get("/health").status_code == 200

                app.state.settings = Settings(environment="development")
                dev_resp = client.get(doc_path)
                assert dev_resp.status_code == 200
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings

    def test_trusted_proxy_xff_resolution_and_unauth_bucket_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REDIS_URI", raising=False)
        reload_settings()

        prev_settings = getattr(app.state, "settings", None)
        test_settings = Settings(
            auth=AuthConfig(api_key="valid-token"),
            ratelimit=RateLimitConfig(
                enabled=True,
                default_limit="2/minute",
                download_limit="3/minute",
                trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
            ),
        )
        mock_http = AsyncMock()
        mock_http.download.return_value = (
            b"<html>ok</html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 15,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            # 1) Trusted ingress (127.0.0.1) resolves rightmost non-trusted XFF hop (203.0.113.5);
            # unauthenticated flood hits 401 -> 429 at default_limit (2/min) without starving
            # authenticated callers, which get their own download_limit (3/min) bucket (`R93`).
            with TestClient(app, client=("127.0.0.1", 50000)) as trusted_client:
                app.state.settings = test_settings
                xff = {"X-Forwarded-For": "198.51.100.99, 203.0.113.5, 10.0.0.2"}
                r1 = trusted_client.get("/https://example.com", headers=xff)
                r2 = trusted_client.get("/https://example.com", headers=xff)
                r3 = trusted_client.get("/https://example.com", headers=xff)
                assert r1.status_code == 401
                assert r1.headers["X-RateLimit-Limit"] == "2"
                assert r2.status_code == 401
                assert r3.status_code == 429

                authed_headers = {**xff, "Authorization": "Bearer valid-token"}
                a1 = trusted_client.get("/https://example.com", headers=authed_headers)
                a2 = trusted_client.get("/https://example.com", headers=authed_headers)
                a3 = trusted_client.get("/https://example.com", headers=authed_headers)
                a4 = trusted_client.get("/https://example.com", headers=authed_headers)
                assert a1.status_code == 200
                assert a1.headers["X-RateLimit-Limit"] == "3"
                assert a2.status_code == 200
                assert a3.status_code == 200
                assert a4.status_code == 429

            # 2) Direct untrusted peer (198.51.100.77) cannot spoof X-Forwarded-For to bypass limits
            with TestClient(app, client=("198.51.100.77", 50000)) as direct_client:
                app.state.settings = test_settings
                h1 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.1",
                }
                h2 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.2",
                }
                h3 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.3",
                }
                h4 = {
                    "Authorization": "Bearer valid-token",
                    "X-Forwarded-For": "192.0.2.4",
                }
                assert direct_client.get("/https://example.com", headers=h1).status_code == 200
                assert direct_client.get("/https://example.com", headers=h2).status_code == 200
                assert direct_client.get("/https://example.com", headers=h3).status_code == 200
                assert direct_client.get("/https://example.com", headers=h4).status_code == 429
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    @pytest.mark.asyncio
    async def test_ssrf_safe_network_backend_pins_ip_blocks_rebinding_and_maps_oserror(
        self,
    ) -> None:
        inner = MagicMock(spec=httpcore.AsyncNetworkBackend)
        fake_stream = AsyncMock(spec=httpcore.AsyncNetworkStream)
        fake_stream.start_tls = AsyncMock(return_value=fake_stream)
        inner.connect_tcp = AsyncMock(return_value=fake_stream)

        backend = SSRFSafeNetworkBackend(inner=inner)

        # Public DNS resolution pins connect_tcp to resolved IP and preserves SNI hostname
        with patch(
            "downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            return_value=["93.184.215.14"],
        ):
            await backend.connect_tcp("example.com", 443, timeout=5.0)
            inner.connect_tcp.assert_awaited_once()
            assert inner.connect_tcp.await_args.kwargs["host"] == "93.184.215.14"
            assert inner.connect_tcp.await_args.kwargs["port"] == 443

        # DNS rebinding to cloud metadata IP at connect time raises SSRFProtectionError
        with patch(
            "downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            side_effect=SSRFProtectionError("Access to cloud metadata endpoints is blocked"),
        ):
            with pytest.raises(SSRFProtectionError):
                await backend.connect_tcp("rebind.example.com", 80)

        # Raw OSError from socket connect is converted into httpcore.ConnectError (-> HTTPClientError)
        inner.connect_tcp = AsyncMock(side_effect=OSError("Network unreachable"))
        with patch(
            "downloader.ssrf_transport.resolve_and_validate_host_async",
            new_callable=AsyncMock,
            return_value=["93.184.215.14"],
        ):
            with pytest.raises(httpcore.ConnectError):
                await backend.connect_tcp("example.com", 443)

        async with HTTPClient(
            timeout=2.0,
            transport=httpx.MockTransport(
                lambda req: (_ for _ in ()).throw(httpx.ConnectError("connect failed", request=req))
            ),
        ) as client:
            with pytest.raises(HTTPClientError):
                await client.download("https://example.com")

    @pytest.mark.asyncio
    async def test_pinned_backend_validates_all_dns_records_and_falls_over_public_ips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SSRFSafeNetworkBackend validates every getaddrinfo record before connecting (`T-1`)."""
        import socket

        from downloader.config import SSRFConfig
        from downloader.validation import EgressPolicy

        inner = MagicMock(spec=httpcore.AsyncNetworkBackend)
        fake_stream = AsyncMock(spec=httpcore.AsyncNetworkStream)
        inner.connect_tcp = AsyncMock(return_value=fake_stream)
        policy = EgressPolicy(Settings(ssrf=SSRFConfig(resolve_dns=True, block_private_ips=True)))
        backend = SSRFSafeNetworkBackend(inner=inner, egress_policy=policy)

        # Public first record + metadata second record -> rejected, zero TCP connect attempts
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda _h, port, *_a, **_kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port or 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port or 443)),
            ],
        )
        with pytest.raises(SSRFProtectionError, match="cloud metadata"):
            await backend.connect_tcp("rebind-dual.example.com", 443)
        inner.connect_tcp.assert_not_awaited()

        # Two valid public records -> validates both and falls over when first fails to connect
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda _h, port, *_a, **_kw: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", port or 443)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", port or 443)),
            ],
        )
        inner.connect_tcp = AsyncMock(
            side_effect=[httpcore.ConnectError("first unreachable"), fake_stream]
        )
        stream = await backend.connect_tcp("multi-public.example.com", 443)
        assert stream is fake_stream
        assert [c.kwargs["host"] for c in inner.connect_tcp.await_args_list] == [
            "93.184.215.14",
            "1.1.1.1",
        ]

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://[::1]/admin",
            "http://[::ffff:127.0.0.1]/admin",
            "http://[::ffff:169.254.169.254]/latest/meta-data/",
            "http://0x7f000001/",
            "http://2130706433/",
            "http://0177.0.0.1/",
            "http://127.1/",
            "http://0177.0.0.08/",
            "http://999.999.999.999/",
            "http://sub.localhost/internal",
        ],
    )
    def test_ssrf_ip_literal_and_hostname_variants_blocked(self, blocked_url: str) -> None:
        with pytest.raises(URLValidationError):
            validate_url(blocked_url)

    def test_rotating_leftmost_xff_hop_stays_in_one_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("REDIS_URI", raising=False)
        reload_settings()

        prev_settings = getattr(app.state, "settings", None)
        test_settings = Settings(
            auth=AuthConfig(api_key="valid-token"),
            ratelimit=RateLimitConfig(
                enabled=True,
                default_limit="2/minute",
                download_limit="2/minute",
                trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
            ),
        )
        mock_http = AsyncMock()
        mock_http.download.return_value = (
            b"<html>ok</html>",
            {
                "url": "https://example.com",
                "status_code": 200,
                "content_type": "text/html",
                "size": 15,
                "headers": {"content-type": "text/html"},
            },
        )
        mock_robots = AsyncMock()
        mock_robots.check_allowed.return_value = None

        app.dependency_overrides[get_http_client] = lambda: mock_http
        app.dependency_overrides[get_robots_checker] = lambda: mock_robots
        try:
            with TestClient(app, client=("127.0.0.1", 50000)) as trusted_client:
                app.state.settings = test_settings
                r1 = trusted_client.get(
                    "/https://example.com",
                    headers={"X-Forwarded-For": "198.51.100.1, 203.0.113.5, 10.0.0.2"},
                )
                r2 = trusted_client.get(
                    "/https://example.com",
                    headers={"X-Forwarded-For": "198.51.100.2, 203.0.113.5, 10.0.0.2"},
                )
                r3 = trusted_client.get(
                    "/https://example.com",
                    headers={"X-Forwarded-For": "198.51.100.3, 203.0.113.5, 10.0.0.2"},
                )
                assert r1.status_code == 401
                assert r2.status_code == 401
                assert r3.status_code == 429
        finally:
            if prev_settings is None:
                if hasattr(app.state, "settings"):
                    delattr(app.state, "settings")
            else:
                app.state.settings = prev_settings
            app.dependency_overrides.pop(get_http_client, None)
            app.dependency_overrides.pop(get_robots_checker, None)

    @pytest.mark.parametrize(
        "peer_ip, xff_header, expected_ip",
        [
            ("127.0.0.1", "198.51.100.99, 203.0.113.5, 10.0.0.2", "203.0.113.5"),
            ("::ffff:127.0.0.1", "203.0.113.5, ::ffff:10.0.0.2", "203.0.113.5"),
            ("127.0.0.1", "203.0.113.5:1234, 10.0.0.2:8080", "203.0.113.5"),
            ("127.0.0.1", "[2001:db8::1]:443, 10.0.0.2", "2001:db8::1"),
            ("127.0.0.1", "10.0.0.3, 10.0.0.2", "10.0.0.3"),
            ("198.51.100.77", "203.0.113.5, 10.0.0.2", "198.51.100.77"),
        ],
    )
    def test_resolve_client_ip_table(self, peer_ip: str, xff_header: str, expected_ip: str) -> None:
        from downloader.ratelimit_middleware import RateLimitMiddleware

        cfg = Settings(
            ratelimit=RateLimitConfig(
                enabled=True,
                trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
            )
        )
        with patch("downloader.ratelimit_middleware.get_settings", return_value=cfg):
            mw = RateLimitMiddleware(MagicMock())

        req = MagicMock()
        req.client = MagicMock(host=peer_ip)
        req.headers = {"x-forwarded-for": xff_header}
        assert mw._resolve_client_ip(req) == expected_ip

    def test_xff_unparsable_hop_is_not_skipped(self) -> None:
        from downloader.ratelimit_middleware import RateLimitMiddleware

        cfg = Settings(
            ratelimit=RateLimitConfig(
                enabled=True,
                trusted_proxies=["127.0.0.1", "10.0.0.0/8"],
            )
        )
        with patch("downloader.ratelimit_middleware.get_settings", return_value=cfg):
            mw = RateLimitMiddleware(MagicMock())

        # Unparsable 'unknown' stops the right-to-left walk without reaching spoofed leftmost IP
        req_after_trusted = MagicMock()
        req_after_trusted.client = MagicMock(host="127.0.0.1")
        req_after_trusted.headers = {"x-forwarded-for": "198.51.100.99, unknown, 10.0.0.2"}
        assert mw._resolve_client_ip(req_after_trusted) == "10.0.0.2"

        req_rightmost_bad = MagicMock()
        req_rightmost_bad.client = MagicMock(host="127.0.0.1")
        req_rightmost_bad.headers = {"x-forwarded-for": "198.51.100.99, not-an-ip"}
        assert mw._resolve_client_ip(req_rightmost_bad) == "127.0.0.1"

    def test_redis_outage_falls_back_to_memory_window(self) -> None:
        """Redis backend outage (`redis.exceptions.ConnectionError`) falls back to memory (`R85`)."""
        import redis.exceptions as redis_exc
        from fastapi import FastAPI

        from downloader.ratelimit_middleware import RateLimitMiddleware

        broken_strategy = MagicMock()
        broken_strategy.hit = AsyncMock(side_effect=redis_exc.ConnectionError("redis down"))
        broken_strategy.get_window_stats = AsyncMock(
            side_effect=redis_exc.ConnectionError("redis down")
        )

        cfg = Settings(
            ratelimit=RateLimitConfig(
                enabled=True,
                storage_uri="redis://127.0.0.1:6379",
                status_limit="2/minute",
                default_limit="2/minute",
            )
        )
        mini_app = FastAPI()

        @mini_app.get("/health")
        async def _health() -> dict[str, str]:
            return {"status": "ok"}

        mini_app.state.settings = cfg
        mini_app.add_middleware(RateLimitMiddleware)

        with patch.object(RateLimitMiddleware, "_get_strategy", return_value=broken_strategy):
            with TestClient(mini_app, raise_server_exceptions=True) as client:
                r1 = client.get("/health")
                r2 = client.get("/health")
                r3 = client.get("/health")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429
        assert r3.json()["detail"]["error_type"] == "rate_limit_exceeded"

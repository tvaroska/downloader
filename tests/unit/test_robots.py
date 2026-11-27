"""Offline unit tests for stateless-first RobotsChecker and DomainThrottle."""

from __future__ import annotations

import asyncio
import tracemalloc
from collections.abc import AsyncIterator

import brotli
import httpx
import pytest

from downloader.crawling.robots import RobotsChecker, RobotsDisallowedError
from downloader.crawling.throttle import DomainThrottle
from downloader.metrics import get_metrics_collector
from downloader.validation import SSRFProtectionError
from tests.fixtures.mock_fixtures import FakeRedis

FakeAsyncRedis = FakeRedis


@pytest.mark.unit
class TestRobotsChecker:
    """Unit tests for RobotsChecker evaluation, caching, fail-open, and SSRF safety."""

    async def test_allow_and_disallow_rules(self) -> None:
        robots_txt = "User-agent: *\nAllow: /public/\nDisallow: /private/\n"

        async def fetcher(url: str) -> tuple[int, str]:
            assert url == "https://example.com/robots.txt"
            return 200, robots_txt

        checker = RobotsChecker(user_agent="REST-API-Downloader/1.0", fetcher=fetcher)
        delay = await checker.check_allowed("https://example.com/public/page.html")
        assert delay is None

        with pytest.raises(RobotsDisallowedError) as exc_info:
            await checker.check_allowed("https://example.com/private/secret.html")

        err = exc_info.value
        assert err.url == "https://example.com/private/secret.html"
        assert err.user_agent == "REST-API-Downloader/1.0"
        assert "REST-API-Downloader/1.0" in str(err)

    async def test_user_agent_precedence_and_override(self) -> None:
        robots_txt = (
            "User-agent: REST-API-Downloader\n"
            "Disallow: /bot-blocked/\n"
            "Crawl-delay: 1.5\n\n"
            "User-agent: CustomResearchBot\n"
            "Allow: /\n"
            "Crawl-delay: 0.25\n\n"
            "User-agent: *\n"
            "Disallow: /\n"
        )

        async def fetcher(_url: str) -> tuple[int, str]:
            return 200, robots_txt

        checker = RobotsChecker(user_agent="REST-API-Downloader/1.0", fetcher=fetcher)

        # Default UA matches REST-API-Downloader group (allowed on /articles, blocked on /bot-blocked)
        delay = await checker.check_allowed("https://example.com/articles/1")
        assert delay == pytest.approx(1.5)

        with pytest.raises(RobotsDisallowedError):
            await checker.check_allowed("https://example.com/bot-blocked/item")

        # Per-call User-Agent override matches CustomResearchBot group
        override_delay = await checker.check_allowed(
            "https://example.com/bot-blocked/item",
            user_agent="CustomResearchBot/2.0",
        )
        assert override_delay == pytest.approx(0.25)

    async def test_crawl_delay_extraction_and_max_cap(self) -> None:
        robots_txt = "User-agent: *\nAllow: /\nCrawl-delay: 120\n"

        async def fetcher(_url: str) -> tuple[int, str]:
            return 200, robots_txt

        checker = RobotsChecker(max_crawl_delay=4.5, fetcher=fetcher)
        delay = await checker.check_allowed("https://example.com/index.html")
        assert delay == pytest.approx(4.5)

    @pytest.mark.parametrize(
        "scenario",
        ["timeout", "network_error", "http_500", "http_404", "malformed_html", "malformed_garbage"],
    )
    async def test_fail_open_and_cached_sentinel(self, scenario: str) -> None:
        calls = 0

        async def fetcher(_url: str) -> tuple[int, str]:
            nonlocal calls
            calls += 1
            if scenario == "timeout":
                raise httpx.ReadTimeout("robots.txt timed out")
            if scenario == "network_error":
                raise httpx.ConnectError("connection refused")
            if scenario == "http_500":
                return 500, "Internal Server Error"
            if scenario == "http_404":
                return 404, "Not Found"
            if scenario == "malformed_html":
                return 200, "<!doctype html><html><body>Gateway error</body></html>"
            return 200, "completely invalid non-directive garbage without colons"

        collector = get_metrics_collector()
        hits_before = collector._counters.get("robots_cache_hits_total", 0)

        checker = RobotsChecker(fetcher=fetcher)
        # First call fails open and caches the allow_all=True sentinel
        assert await checker.check_allowed("https://example.com/any/path") is None
        assert calls == 1

        # Second call hits cached fail-open sentinel without re-fetching
        assert await checker.check_allowed("https://example.com/another/path") is None
        assert calls == 1
        assert collector._counters.get("robots_cache_hits_total", 0) == hits_before + 1

    async def test_bounded_lru_eviction_and_ttl_expiry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = 1000.0
        monkeypatch.setattr("downloader.crawling.robots.time.monotonic", lambda: now)

        fetched_origins: list[str] = []

        async def fetcher(url: str) -> tuple[int, str]:
            fetched_origins.append(url)
            return 200, "User-agent: *\nAllow: /\n"

        checker = RobotsChecker(
            robots_cache_ttl=60,
            robots_cache_max_size=2,
            fetcher=fetcher,
        )

        await checker.check_allowed("https://example.com/1")
        await checker.check_allowed("https://test.com/1")
        assert len(fetched_origins) == 2

        # Touch example.com so test.com becomes LRU
        await checker.check_allowed("https://example.com/2")
        assert len(fetched_origins) == 2

        # Adding success.com evicts test.com (maxsize=2)
        await checker.check_allowed("https://success.com/1")
        assert len(fetched_origins) == 3

        # example.com is still cached; test.com was evicted and must re-fetch
        await checker.check_allowed("https://example.com/3")
        assert len(fetched_origins) == 3
        await checker.check_allowed("https://test.com/2")
        assert len(fetched_origins) == 4

        # Advance monotonic clock past TTL (60s) -> entry expires and re-fetches
        now += 61.0
        await checker.check_allowed("https://test.com/3")
        assert len(fetched_origins) == 5

    async def test_memory_vs_mocked_redis_cache_parity_and_fallback(self) -> None:
        robots_txt = "User-agent: *\nDisallow: /secret\nCrawl-delay: 2.0\n"
        mem_fetches = 0
        redis_fetches = 0

        async def mem_fetcher(_url: str) -> tuple[int, str]:
            nonlocal mem_fetches
            mem_fetches += 1
            return 200, robots_txt

        async def redis_fetcher(_url: str) -> tuple[int, str]:
            nonlocal redis_fetches
            redis_fetches += 1
            return 200, robots_txt

        fake_redis = FakeRedis()
        mem_checker = RobotsChecker(fetcher=mem_fetcher, redis_client=None)
        redis_checker_writer = RobotsChecker(fetcher=redis_fetcher, redis_client=fake_redis)
        redis_checker_reader = RobotsChecker(fetcher=redis_fetcher, redis_client=fake_redis)

        # Both return identical Crawl-delay on allowed URL
        assert await mem_checker.check_allowed("https://example.com/ok") == pytest.approx(2.0)
        assert await redis_checker_writer.check_allowed("https://example.com/ok") == pytest.approx(
            2.0
        )
        assert "robots:v2:https:example.com" in fake_redis.store

        # HTTP vs HTTPS origins remain isolated in Redis and memory caches
        assert await redis_checker_writer.check_allowed("http://example.com/ok") == pytest.approx(
            2.0
        )
        assert "robots:v2:http:example.com" in fake_redis.store
        assert redis_fetches == 2

        # Second pod (redis_checker_reader) reads shared Redis entry without fetching
        assert await redis_checker_reader.check_allowed("https://example.com/ok2") == pytest.approx(
            2.0
        )
        assert redis_fetches == 2
        assert mem_fetches == 1

        # Both raise identical RobotsDisallowedError on disallowed path
        with pytest.raises(RobotsDisallowedError):
            await mem_checker.check_allowed("https://example.com/secret")
        with pytest.raises(RobotsDisallowedError):
            await redis_checker_reader.check_allowed("https://example.com/secret")

        # When Redis read fails, checker degrades cleanly to in-memory cache
        fake_redis.fail_get = True
        assert await redis_checker_writer.check_allowed("https://example.com/ok3") == pytest.approx(
            2.0
        )
        assert redis_fetches == 2

    async def test_ssrf_protection_on_origin_and_redirect(self) -> None:
        checker = RobotsChecker()
        try:
            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("http://169.254.169.254/latest/meta-data/")

            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("http://127.0.0.1:8080/internal")

            # Verify redirect hook inside RobotsChecker blocks 302 -> loopback and fails open (`A-8`)
            requested_urls: list[str] = []

            def handler(request: httpx.Request) -> httpx.Response:
                requested_urls.append(str(request.url))
                return httpx.Response(
                    302,
                    headers={"Location": "http://127.0.0.1:8080/robots.txt"},
                    request=request,
                )

            async with RobotsChecker(transport=httpx.MockTransport(handler)) as redirect_checker:
                with pytest.raises(SSRFProtectionError):
                    await redirect_checker._default_fetch("https://example.com/robots.txt")
                assert await redirect_checker.check_allowed("https://example.com/public") is None
                assert all("127.0.0.1" not in u for u in requested_urls)
        finally:
            await checker.close()

    @pytest.mark.parametrize(
        "blocked_url",
        [
            "http://127.0.0.1/secret",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/internal",
        ],
    )
    async def test_origin_validated_before_any_fetch(self, blocked_url: str) -> None:
        fetch_calls = 0

        async def _fetcher(_url: str) -> tuple[int, str]:
            nonlocal fetch_calls
            fetch_calls += 1
            return 200, "User-agent: *\nAllow: /\n"

        checker = RobotsChecker(fetcher=_fetcher)
        with pytest.raises(SSRFProtectionError):
            await checker.check_allowed(blocked_url)
        assert fetch_calls == 0

    async def test_robots_redirect_body_bomb_is_closed_unread(self) -> None:
        """RobotsChecker closes 3xx streams unread instead of decompressing redirect bodies (`A-1`)."""

        class _TrackingStream(httpx.AsyncByteStream):
            def __init__(self, payload: bytes) -> None:
                self._payload = payload
                self.chunks_read = 0
                self.closed = False

            async def __aiter__(self) -> AsyncIterator[bytes]:
                self.chunks_read += 1
                yield self._payload

            async def aclose(self) -> None:
                self.closed = True

        bomb = brotli.compress(b"\x00" * (16 * 1024 * 1024), quality=6)
        redirect_stream = _TrackingStream(bomb)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(
                    301,
                    headers={"Location": "/robots-v2.txt", "Content-Encoding": "br"},
                    stream=redirect_stream,
                    request=request,
                )
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                stream=httpx.ByteStream(b"User-agent: *\nDisallow: /secret\n"),
                request=request,
            )

        tracemalloc.start()
        try:
            async with RobotsChecker(transport=httpx.MockTransport(handler)) as checker:
                with pytest.raises(RobotsDisallowedError):
                    await checker.check_allowed("https://example.com/secret/page")
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert redirect_stream.chunks_read == 0
        assert redirect_stream.closed is True
        assert peak_bytes < 2 * 1024 * 1024

        loop_streams: list[_TrackingStream] = []

        def loop_handler(request: httpx.Request) -> httpx.Response:
            st = _TrackingStream(bomb)
            loop_streams.append(st)
            return httpx.Response(
                302,
                headers={"Location": "/robots.txt", "Content-Encoding": "br"},
                stream=st,
                request=request,
            )

        async with RobotsChecker(transport=httpx.MockTransport(loop_handler)) as loop_checker:
            assert await loop_checker.check_allowed("https://example.com/secret/page") is None
            assert len(loop_streams) == 6
            assert all(st.chunks_read == 0 and st.closed for st in loop_streams)
            assert await loop_checker.check_allowed("https://example.com/secret/page") is None
            assert len(loop_streams) == 6


@pytest.mark.unit
class TestDomainThrottle:
    """Unit tests for per-domain Crawl-delay pacing across memory and mocked-Redis backends."""

    async def test_in_memory_domain_pacing_and_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mono = 500.0
        slept: list[float] = []

        monkeypatch.setattr("downloader.crawling.throttle.time.monotonic", lambda: fake_mono)

        async def fake_sleep(seconds: float) -> None:
            nonlocal fake_mono
            slept.append(seconds)
            fake_mono += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        throttle = DomainThrottle(max_crawl_delay=5.0, max_size=2)

        # First call to example.com proceeds immediately
        w1 = await throttle.wait_if_needed("https://example.com/a", 1.5)
        assert w1 == 0.0
        assert slept == []

        # Different domain proceeds immediately without waiting on example.com
        w_other = await throttle.wait_if_needed("https://test.com/a", 2.0)
        assert w_other == 0.0
        assert slept == []

        # Second call to example.com waits full 1.5s slot
        w2 = await throttle.wait_if_needed("example.com", 1.5)
        assert w2 == pytest.approx(1.5)
        assert slept == [pytest.approx(1.5)]

        # Crawl-delay exceeding max_crawl_delay is capped at 5.0s
        await throttle.wait_if_needed("https://success.com/1", 60.0)
        w_capped = await throttle.wait_if_needed("https://success.com/2", 60.0)
        assert w_capped == pytest.approx(5.0)

        # Bounded LRU evicts oldest domain when max_size=2 is exceeded
        assert len(throttle._next_allowed) <= 2

    async def test_mocked_redis_domain_pacing_and_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_wall = 1_700_000_000.0
        fake_mono = 2000.0
        slept: list[float] = []

        monkeypatch.setattr("downloader.crawling.throttle.time.time", lambda: fake_wall)
        monkeypatch.setattr("downloader.crawling.throttle.time.monotonic", lambda: fake_mono)

        async def fake_sleep(seconds: float) -> None:
            nonlocal fake_wall, fake_mono
            slept.append(seconds)
            fake_wall += seconds
            fake_mono += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        fake_redis = FakeRedis()
        pod_a = DomainThrottle(max_crawl_delay=10.0, redis_client=fake_redis)
        pod_b = DomainThrottle(max_crawl_delay=10.0, redis_client=fake_redis)

        # Pod A reserves initial slot on example.com (0s wait)
        assert await pod_a.wait_if_needed("example.com", 0.8) == 0.0
        assert "throttle:v1:example.com" in fake_redis.store

        # Pod B immediately sees Pod A's Redis slot and paces by 0.8s
        w_b = await pod_b.wait_if_needed("example.com", 0.8)
        assert w_b == pytest.approx(0.8)
        assert slept == [pytest.approx(0.8)]

        # Zero / None crawl_delay is a no-op
        assert await pod_a.wait_if_needed("example.com", None) == 0.0
        assert await pod_a.wait_if_needed("example.com", 0.0) == 0.0

        # Redis failure falls back cleanly to in-memory slot without raising
        fake_redis.fail_get = True
        assert await pod_a.wait_if_needed("fallback.example.com", 0.5) == 0.0
        w_fallback = await pod_a.wait_if_needed("fallback.example.com", 0.5)
        assert w_fallback == pytest.approx(0.5)


@pytest.mark.unit
async def test_failure_sentinel_uses_short_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient 5xx/timeout/DNS failures cache allow-all for ~60s rather than 3600s (`R47`)."""
    from downloader.config import Settings
    from downloader.validation import DNSResolutionError, EgressPolicy

    now = 5_000.0
    monkeypatch.setattr("downloader.crawling.robots.time.monotonic", lambda: now)

    responses: list[tuple[int, str]] = [
        (503, "Service Unavailable"),
        (200, "User-agent: *\nDisallow: /private/\n"),
    ]
    fetch_calls = 0

    async def fetcher(_url: str) -> tuple[int, str]:
        nonlocal fetch_calls
        idx = min(fetch_calls, len(responses) - 1)
        fetch_calls += 1
        return responses[idx]

    fake_redis = FakeRedis()
    redis_checker = RobotsChecker(
        robots_cache_ttl=3600,
        negative_cache_ttl=60,
        redis_client=fake_redis,
        fetcher=fetcher,
    )
    assert await redis_checker.check_allowed("https://example.com/private/doc") is None
    assert fetch_calls == 1
    _, redis_ttl = fake_redis.store["robots:v2:https:example.com"]
    assert redis_ttl == pytest.approx(60.0)

    # Reset fetcher counter and exercise in-memory negative TTL (~60s vs 3600s positive TTL)
    fetch_calls = 0
    mem_checker = RobotsChecker(
        robots_cache_ttl=3600,
        negative_cache_ttl=60,
        redis_client=None,
        fetcher=fetcher,
    )
    assert await mem_checker.check_allowed("https://example.com/private/doc") is None
    assert fetch_calls == 1

    now += 30.0
    assert await mem_checker.check_allowed("https://example.com/private/doc") is None
    assert fetch_calls == 1

    # Past 60s negative TTL (well before 3600s positive TTL), sentinel expires and re-fetches 200 OK
    now += 31.0
    with pytest.raises(RobotsDisallowedError):
        await mem_checker.check_allowed("https://example.com/private/doc")
    assert fetch_calls == 2

    # Positive 200 OK entry stays cached well beyond 60s (up to 3600s)
    now += 300.0
    with pytest.raises(RobotsDisallowedError):
        await mem_checker.check_allowed("https://example.com/private/doc")
    assert fetch_calls == 2

    # Typed DNSResolutionError fails open cleanly while SSRFProtectionError still propagates
    class _DNSFailPolicy(EgressPolicy):
        async def validate_url(self, url: str) -> str:
            raise DNSResolutionError(f"NXDOMAIN for {url}")

    dns_checker = RobotsChecker(egress_policy=_DNSFailPolicy(Settings()))
    assert await dns_checker.check_allowed("https://unresolvable.invalid/path") is None


@pytest.mark.unit
@pytest.mark.parametrize("failure_kind", ["timeout", "dns", "malformed_html", "malformed_garbage"])
async def test_failure_sentinel_uses_short_ttl_for_timeout_dns_and_malformed(
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """Timeout, DNS, and malformed robots.txt responses cache fail-open for ~60s, not 3600s (`R91`)."""
    from downloader.validation import DNSResolutionError

    now = 9_000.0
    monkeypatch.setattr("downloader.crawling.robots.time.monotonic", lambda: now)

    calls = 0

    async def fetcher(_url: str) -> tuple[int, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            if failure_kind == "timeout":
                raise httpx.ReadTimeout("robots fetch timed out")
            if failure_kind == "dns":
                raise DNSResolutionError("example.com")
            if failure_kind == "malformed_html":
                return 200, "<!DOCTYPE html><html><body>Error page</body></html>"
            return 200, "garbage without directives or colons"
        return 200, "User-agent: *\nDisallow: /private/\n"

    # 1. Redis backend stores short 60s TTL (not 3600s)
    fake_redis = FakeRedis()
    redis_checker = RobotsChecker(
        robots_cache_ttl=3600,
        negative_cache_ttl=60,
        redis_client=fake_redis,
        fetcher=fetcher,
    )
    assert await redis_checker.check_allowed("https://example.com/private/doc") is None
    assert calls == 1
    _, redis_ttl = fake_redis.store["robots:v2:https:example.com"]
    assert redis_ttl == pytest.approx(60.0)

    # 2. In-memory cache expires after 60s negative TTL and re-fetches rules
    calls = 0
    mem_checker = RobotsChecker(
        robots_cache_ttl=3600,
        negative_cache_ttl=60,
        redis_client=None,
        fetcher=fetcher,
    )
    assert await mem_checker.check_allowed("https://example.com/private/doc") is None
    assert calls == 1

    now += 30.0
    assert await mem_checker.check_allowed("https://example.com/private/doc") is None
    assert calls == 1

    now += 31.0
    with pytest.raises(RobotsDisallowedError):
        await mem_checker.check_allowed("https://example.com/private/doc")
    assert calls == 2


@pytest.mark.unit
async def test_injected_fetcher_strips_utf8_bom_and_redis_cache_avoids_reparse() -> None:
    """UTF-8 BOM is stripped on injected fetcher (`R112`) and Redis cache reuses parsed rules (`R114`)."""
    bom_robots = "\ufeffUser-agent: *\nDisallow: /secret/\n"

    async def fetcher(_url: str) -> tuple[int, str]:
        return 200, bom_robots

    fake_redis = FakeRedis()
    writer = RobotsChecker(fetcher=fetcher, redis_client=fake_redis)
    with pytest.raises(RobotsDisallowedError):
        await writer.check_allowed("https://example.com/secret/item")

    reader = RobotsChecker(fetcher=fetcher, redis_client=fake_redis)
    with pytest.raises(RobotsDisallowedError):
        await reader.check_allowed("https://example.com/secret/first")

    # Second read on the same pod hits in-memory parsed entry without calling Redis again.
    fake_redis.fail_get = True
    with pytest.raises(RobotsDisallowedError):
        await reader.check_allowed("https://example.com/secret/second")


@pytest.mark.unit
async def test_cache_key_excludes_userinfo(caplog: pytest.LogCaptureFixture) -> None:
    """Origin keys strip userinfo so credentials never fragment caches or leak to logs (`R47`)."""
    import logging

    caplog.set_level(logging.DEBUG, logger="downloader.crawling.robots")
    fetched_urls: list[str] = []

    async def fetcher(url: str) -> tuple[int, str]:
        fetched_urls.append(url)
        return 503, "Upstream down"

    fake_redis = FakeRedis()
    checker = RobotsChecker(
        max_cache_bytes=2048,
        redis_client=fake_redis,
        fetcher=fetcher,
    )

    secret_token = "super-secret-pass-987"
    url_with_creds = f"https://alice:{secret_token}@EXAMPLE.COM.:443/account"
    assert await checker.check_allowed(url_with_creds) is None
    assert await checker.check_allowed("https://example.com/other") is None

    assert fetched_urls == ["https://example.com/robots.txt"]
    assert list(checker._cache.keys()) == [("https", "example.com", 443)]
    assert "robots:v2:https:example.com" in fake_redis.store
    assert all(secret_token not in str(k) for k in fake_redis.store)
    assert secret_token not in caplog.text

    # Byte-bounded LRU evicts older entries when max_cache_bytes is exceeded
    big_rules = "User-agent: *\n" + ("Disallow: /very-long-disallowed-prefix-\n" * 40)

    async def big_fetcher(_url: str) -> tuple[int, str]:
        return 200, big_rules

    bounded_checker = RobotsChecker(max_cache_bytes=2048, fetcher=big_fetcher)
    for idx in range(5):
        await bounded_checker.check_allowed(f"https://example{idx}.com/page")
    assert bounded_checker._cached_bytes <= 2048


@pytest.mark.unit
async def test_wildcard_and_longest_match_rules() -> None:
    """RFC 9309 `*` wildcards, `$` end anchors, longest-match precedence, and cross-origin redirect checks (`R47`)."""
    from downloader.http_client import HTTPClient

    robots_txt = (
        "User-agent: *\n"
        "Disallow: /docs/\n"
        "Allow: /docs/public/\n"
        "Disallow: /*.pdf$\n"
        "Allow: /tie-path\n"
        "Disallow: /tie-path\n\n"
        "User-agent: REST-API-Downloader\n"
        "Disallow: /bot-only/*.json$\n"
        "Allow: /bot-only/public.json$\n"
    )

    async def fetcher(url: str) -> tuple[int, str]:
        if url == "https://test.com/robots.txt":
            return 200, "User-agent: *\nDisallow: /blocked-target\n"
        return 200, robots_txt

    checker = RobotsChecker(user_agent="REST-API-Downloader/1.0", fetcher=fetcher)

    # Specific group overrides `*` group for REST-API-Downloader
    assert await checker.check_allowed("https://example.com/docs/secret.html") is None
    assert await checker.check_allowed("https://example.com/bot-only/public.json") is None
    assert await checker.check_allowed("https://example.com/bot-only/data.json?raw=1") is None
    with pytest.raises(RobotsDisallowedError):
        await checker.check_allowed("https://example.com/bot-only/private.json")

    # Wildcard `*` group rules for a generic User-Agent
    assert (
        await checker.check_allowed(
            "https://example.com/docs/public/guide.html", user_agent="OtherBot/1.0"
        )
        is None
    )
    with pytest.raises(RobotsDisallowedError):
        await checker.check_allowed(
            "https://example.com/docs/internal/guide.html", user_agent="OtherBot/1.0"
        )

    # `$` anchor blocks exact `.pdf` suffix but allows `.pdf?dl=0` or `.pdf.html`
    with pytest.raises(RobotsDisallowedError):
        await checker.check_allowed(
            "https://example.com/files/report.pdf", user_agent="OtherBot/1.0"
        )
    assert (
        await checker.check_allowed(
            "https://example.com/files/report.pdf?preview=1", user_agent="OtherBot/1.0"
        )
        is None
    )
    # Equal-length Allow/Disallow tie resolves to Allow per RFC 9309 §2.2.2
    assert (
        await checker.check_allowed("https://example.com/tie-path/sub", user_agent="OtherBot/1.0")
        is None
    )

    # Cross-origin redirect enforces target origin's robots.txt via HTTPClient redirect_policy
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"Location": "https://test.com/blocked-target"},
                request=request,
            )
        return httpx.Response(200, text="should not reach", request=request)

    async def check_cross_origin(prev_url: str, next_url: str) -> None:
        if prev_url != next_url:
            await checker.check_allowed(next_url)

    async with HTTPClient(
        transport=httpx.MockTransport(redirect_handler),
        redirect_policy=check_cross_origin,
    ) as client:
        with pytest.raises(RobotsDisallowedError):
            await client.download("https://example.com/start")


@pytest.mark.unit
async def test_robots_redirect_to_blocked_address_fails_open_and_caches_sentinel() -> None:
    """`A-8`: `robots.txt` redirect to a blocked private/metadata address fails open with 60s sentinel."""
    seen_urls: list[str] = []

    def _redirect_to_blocked(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(
                302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
                request=request,
            )
        raise AssertionError(f"Blocked redirect target must never be requested: {request.url}")

    fake_redis = FakeRedis(decode_responses=True)
    checker = RobotsChecker(
        transport=httpx.MockTransport(_redirect_to_blocked),
        negative_cache_ttl=60,
        redis_client=fake_redis,
    )
    try:
        assert await checker.check_allowed("https://example.com/page-1") is None
        assert await checker.check_allowed("https://example.com/page-2") is None
        assert seen_urls == ["https://example.com/robots.txt"]
    finally:
        await checker.close()


@pytest.mark.unit
async def test_robots_product_token_exact_equality_and_wildcard_fallback() -> None:
    """`A-9`: `User-agent` product tokens compare by case-insensitive equality rather than substring."""
    robots_txt = (
        "User-agent: api\n"
        "Disallow: /\n\n"
        "User-agent: load\n"
        "Disallow: /\n\n"
        "User-agent: e\n"
        "Disallow: /\n\n"
        "User-agent: *\n"
        "Allow: /public\n"
        "Disallow: /wildcard-blocked\n"
    )

    async def fetcher(_url: str) -> tuple[int, str]:
        return 200, robots_txt

    checker = RobotsChecker(user_agent="REST-API-Downloader/1.0", fetcher=fetcher)
    assert await checker.check_allowed("https://example.com/public") is None
    with pytest.raises(RobotsDisallowedError):
        await checker.check_allowed("https://example.com/wildcard-blocked")

    exact_robots = (
        "User-agent: rest-api-downloader\nDisallow: /exact-bot-only\n\nUser-agent: *\nAllow: /\n"
    )

    async def exact_fetcher(_url: str) -> tuple[int, str]:
        return 200, exact_robots

    exact_checker = RobotsChecker(
        user_agent="REST-API-Downloader/1.0 (+https://download.example.org)",
        fetcher=exact_fetcher,
    )
    with pytest.raises(RobotsDisallowedError):
        await exact_checker.check_allowed("https://example.com/exact-bot-only")

"""Offline unit tests for stateless-first RobotsChecker and DomainThrottle."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.downloader.crawling.robots import RobotsChecker, RobotsDisallowedError
from src.downloader.crawling.throttle import DomainThrottle
from src.downloader.metrics import get_metrics_collector
from src.downloader.validation import SSRFProtectionError


class FakeAsyncRedis:
    """Minimal in-memory async Redis stub for parity testing."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.fail_get: bool = False
        self.fail_set: bool = False

    async def get(self, key: str) -> bytes | None:
        if self.fail_get:
            raise ConnectionError("simulated redis read failure")
        entry = self.store.get(key)
        if entry is None:
            return None
        return entry[0].encode("utf-8")

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool:
        if self.fail_set:
            raise ConnectionError("simulated redis write failure")
        ttl_s = float(ex) if ex is not None else (float(px) / 1000.0 if px is not None else None)
        self.store[key] = (value, ttl_s)
        return True

    async def psetex(self, key: str, time_ms: int, value: str) -> bool:
        if self.fail_set:
            raise ConnectionError("simulated redis psetex failure")
        self.store[key] = (value, float(time_ms) / 1000.0)
        return True


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
        monkeypatch.setattr("src.downloader.crawling.robots.time.monotonic", lambda: now)

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

        fake_redis = FakeAsyncRedis()
        mem_checker = RobotsChecker(fetcher=mem_fetcher, redis_client=None)
        redis_checker_writer = RobotsChecker(fetcher=redis_fetcher, redis_client=fake_redis)
        redis_checker_reader = RobotsChecker(fetcher=redis_fetcher, redis_client=fake_redis)

        # Both return identical Crawl-delay on allowed URL
        assert await mem_checker.check_allowed("https://example.com/ok") == pytest.approx(2.0)
        assert await redis_checker_writer.check_allowed("https://example.com/ok") == pytest.approx(
            2.0
        )
        assert "robots:v1:example.com" in fake_redis.store

        # Second pod (redis_checker_reader) reads shared Redis entry without fetching
        assert await redis_checker_reader.check_allowed("https://example.com/ok2") == pytest.approx(
            2.0
        )
        assert redis_fetches == 1
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
        assert redis_fetches == 1

    async def test_ssrf_protection_on_origin_and_redirect(self) -> None:
        checker = RobotsChecker()
        try:
            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("http://169.254.169.254/latest/meta-data/")

            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("http://127.0.0.1:8080/internal")

            # Verify redirect hook inside RobotsChecker blocks 302 -> loopback
            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    302,
                    headers={"Location": "http://127.0.0.1:8080/robots.txt"},
                    request=request,
                )

            checker._http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
                event_hooks={"response": [checker._on_redirect_hook]},
            )
            with pytest.raises(SSRFProtectionError):
                await checker.check_allowed("https://example.com/public")
        finally:
            await checker.close()


@pytest.mark.unit
class TestDomainThrottle:
    """Unit tests for per-domain Crawl-delay pacing across memory and mocked-Redis backends."""

    async def test_in_memory_domain_pacing_and_isolation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_mono = 500.0
        slept: list[float] = []

        monkeypatch.setattr("src.downloader.crawling.throttle.time.monotonic", lambda: fake_mono)

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

        monkeypatch.setattr("src.downloader.crawling.throttle.time.time", lambda: fake_wall)
        monkeypatch.setattr("src.downloader.crawling.throttle.time.monotonic", lambda: fake_mono)

        async def fake_sleep(seconds: float) -> None:
            nonlocal fake_wall, fake_mono
            slept.append(seconds)
            fake_wall += seconds
            fake_mono += seconds

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        fake_redis = FakeAsyncRedis()
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

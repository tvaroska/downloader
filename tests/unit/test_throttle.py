"""Unit and route tests for bounded Crawl-delay throttling and canonical host keys (`R48`)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from downloader.crawling.throttle import CrawlDelayExceededError, DomainThrottle
from downloader.dependencies import get_domain_throttle, get_http_client, get_robots_checker
from downloader.main import app
from tests.unit.test_robots import FakeAsyncRedis


@pytest.mark.unit
async def test_wait_is_bounded_and_key_is_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canonicalize host keys, bound queued Crawl-delay waits, refund cancelled slots, and emit 429 (`R48`)."""
    fake_mono = 1_000.0
    fake_wall = 1_700_000_000.0
    slept: list[float] = []

    monkeypatch.setattr("downloader.crawling.throttle.time.monotonic", lambda: fake_mono)
    monkeypatch.setattr("downloader.crawling.throttle.time.time", lambda: fake_wall)

    async def fake_sleep(seconds: float) -> None:
        nonlocal fake_mono, fake_wall
        slept.append(seconds)
        fake_mono += seconds
        fake_wall += seconds

    orig_sleep = asyncio.sleep
    monkeypatch.setattr("downloader.crawling.throttle.asyncio.sleep", fake_sleep)

    throttle = DomainThrottle(max_crawl_delay=10.0, max_wait_seconds=5.0)

    # First request reserves [1000.0 -> 1004.0] with 0s wait
    userinfo_url = "https://" + "user:pw" + "@EXAMPLE.COM.:8443/path"
    assert await throttle.wait_if_needed(userinfo_url, 4.0) == 0.0
    assert list(throttle._next_allowed.keys()) == ["example.com"]

    # Freeze clock inside throttle sleep so concurrent/queued callers pile up against the canonical bucket
    monkeypatch.setattr("downloader.crawling.throttle.asyncio.sleep", AsyncMock())

    # Second variant (`example.com:443`) sees 4.0s <= 5.0s bound -> reserves [1004.0 -> 1008.0]
    w2 = await throttle.wait_if_needed("https://example.com:443/b", 4.0)
    assert w2 == pytest.approx(4.0)

    # Third variant (`EXAMPLE.COM.`) sees 8.0s > 5.0s bound -> raises CrawlDelayExceededError without advancing slot!
    slot_before = throttle._next_allowed["example.com"]
    with pytest.raises(CrawlDelayExceededError) as exc_info:
        await throttle.wait_if_needed("EXAMPLE.COM.", 4.0)
    assert exc_info.value.domain == "example.com"
    assert exc_info.value.retry_after == pytest.approx(8.0)
    assert throttle._next_allowed["example.com"] == pytest.approx(slot_before)

    # CancelledError during sleep refunds the reserved in-memory slot
    cancel_throttle = DomainThrottle(max_crawl_delay=10.0, max_wait_seconds=10.0)
    assert await cancel_throttle.wait_if_needed("example.org", 3.0) == 0.0
    slot_after_first = cancel_throttle._next_allowed["example.org"]

    sleep_started = asyncio.Event()

    async def cancellable_sleep(_seconds: float) -> None:
        sleep_started.set()
        await asyncio.Future()

    monkeypatch.setattr("downloader.crawling.throttle.asyncio.sleep", cancellable_sleep)
    waiter = asyncio.create_task(cancel_throttle.wait_if_needed("https://example.org:8080/x", 3.0))
    await sleep_started.wait()
    assert cancel_throttle._next_allowed["example.org"] == pytest.approx(slot_after_first + 3.0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert cancel_throttle._next_allowed["example.org"] == pytest.approx(slot_after_first)

    # Atomic Redis Lua enforces the exact same max_wait_seconds bound
    fake_redis = FakeAsyncRedis()
    monkeypatch.setattr("downloader.crawling.throttle.asyncio.sleep", AsyncMock())
    redis_throttle = DomainThrottle(
        max_crawl_delay=10.0,
        max_wait_seconds=3.0,
        redis_client=fake_redis,
    )
    assert await redis_throttle.wait_if_needed("https://u:p@Test.COM.:9000/1", 2.5) == 0.0
    assert await redis_throttle.wait_if_needed("test.com", 2.5) == pytest.approx(2.5)
    with pytest.raises(CrawlDelayExceededError) as redis_exc:
        await redis_throttle.wait_if_needed("TEST.COM.", 2.5)
    assert redis_exc.value.retry_after == pytest.approx(5.0)

    # Restore real asyncio.sleep before spinning up TestClient lifespan portal
    monkeypatch.setattr("downloader.crawling.throttle.asyncio.sleep", orig_sleep)

    # Route boundary maps CrawlDelayExceededError -> HTTP 429 with Retry-After header
    mock_robots = MagicMock()
    mock_robots.check_allowed = AsyncMock(return_value=6.0)
    mock_throttle = MagicMock()
    mock_throttle.wait_if_needed = AsyncMock(
        side_effect=CrawlDelayExceededError("example.com", retry_after=6.2)
    )
    mock_http = MagicMock()

    app.dependency_overrides[get_robots_checker] = lambda: mock_robots
    app.dependency_overrides[get_domain_throttle] = lambda: mock_throttle
    app.dependency_overrides[get_http_client] = lambda: mock_http
    try:
        with (
            patch(
                "downloader.routes.download.validate_url_async",
                new=AsyncMock(return_value="https://example.com/slow"),
            ),
            TestClient(app) as client,
        ):
            resp = client.get("/https://example.com/slow")
            assert resp.status_code == 429
            assert resp.headers["retry-after"] == "7"
            assert resp.json()["detail"]["error_type"] == "crawl_delay_exceeded"
    finally:
        app.dependency_overrides.pop(get_robots_checker, None)
        app.dependency_overrides.pop(get_domain_throttle, None)
        app.dependency_overrides.pop(get_http_client, None)

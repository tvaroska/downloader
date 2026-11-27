"""Shared `JobStore` contract suite across `InMemoryJobStore` and `JobManager(FakeRedis)` (`T-11`)."""

from __future__ import annotations

import asyncio
import time

import pytest

from downloader.job_manager import JobManager
from downloader.ports import JobStatus, JobStore
from tests.fixtures.in_memory_ports import InMemoryJobStore
from tests.fixtures.mock_fixtures import FakeRedis


def _build_job_store(kind: str, *, job_ttl: int = 3600) -> tuple[JobStore, FakeRedis | None]:
    if kind == "in_memory":
        return InMemoryJobStore(job_ttl=job_ttl), None
    fake_redis = FakeRedis(decode_responses=True)
    jm = JobManager("redis://localhost:6379", job_ttl=job_ttl)
    jm.redis_client = fake_redis
    return jm, fake_redis


@pytest.mark.unit
@pytest.mark.port
@pytest.mark.parametrize("store_kind", ["in_memory", "redis_fake"])
class TestJobStoreContractParity:
    """Verify `InMemoryJobStore` and `JobManager(FakeRedis)` uphold identical `JobStore` contracts."""

    async def test_lifecycle_and_cancel_prevents_complete(self, store_kind: str) -> None:
        store, _ = _build_job_store(store_kind)
        job_id = await store.create_job({"urls": [{"url": "https://example.com/a"}]})
        info = await store.get_job_info(job_id)
        assert info is not None and info.status == JobStatus.PENDING

        assert await store.update_job_status(job_id, JobStatus.RUNNING, progress=50) is True
        assert await store.cancel_job(job_id) is True

        # Cancelled job must reject subsequent complete_job and keep results absent
        applied = await store.complete_job(
            job_id,
            [{"url": "https://example.com/a", "success": True, "content": "ok"}],
            {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
        )
        assert applied is False
        final_info = await store.get_job_info(job_id)
        assert final_info is not None and final_info.status == JobStatus.CANCELLED
        assert await store.get_job_results(job_id) is None

    async def test_complete_job_enforces_max_result_bytes_identically(
        self, store_kind: str
    ) -> None:
        store, _ = _build_job_store(store_kind)
        job_id = await store.create_job(
            {"urls": [{"url": "https://example.com/1"}, {"url": "https://example.com/2"}]}
        )
        assert await store.update_job_status(job_id, JobStatus.RUNNING) is True
        results = [
            {
                "url": "https://example.com/1",
                "success": True,
                "format": "text",
                "content": "short",
                "size": 5,
            },
            {
                "url": "https://example.com/2",
                "success": True,
                "format": "text",
                "content": "X" * 4096,
                "size": 4096,
            },
        ]
        summary = {"total_requests": 2, "successful_requests": 2, "failed_requests": 0}
        assert await store.complete_job(job_id, results, summary, max_result_bytes=1200) is True

        stored = await store.get_job_results(job_id)
        info = await store.get_job_info(job_id)
        assert stored is not None and info is not None
        assert stored.summary.get("truncated") is True
        assert stored.results[0]["success"] is True
        assert stored.results[1]["success"] is False
        assert stored.results[1]["error_type"] == "content_too_large"
        assert stored.results[1]["status_code"] == 413
        assert info.successful_urls == 1
        assert info.failed_urls == 1

    async def test_job_ttl_expiry_evicts_expired_jobs(
        self, store_kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now_mono = [1000.0]
        now_wall = [1_700_000_000.0]
        monkeypatch.setattr("tests.fixtures.mock_fixtures.time.monotonic", lambda: now_mono[0])
        monkeypatch.setattr("tests.fixtures.in_memory_ports.time.time", lambda: now_wall[0])

        store, _ = _build_job_store(store_kind, job_ttl=5)
        job_id = await store.create_job({"urls": [{"url": "https://example.com/ttl"}]})
        now_mono[0] += 6.0
        now_wall[0] += 6.0
        assert await store.update_job_status(job_id, JobStatus.RUNNING) is False
        assert await store.get_job_info(job_id) is None
        assert await store.get_job_results(job_id) is None


@pytest.mark.unit
async def test_fake_redis_blmove_and_blpop_honour_timeout_and_wakeup() -> None:
    """`T-11`: `FakeRedis.blmove`/`blpop` block up to `timeout` when empty and wake on push."""
    fake = FakeRedis(decode_responses=True)
    started = time.monotonic()
    empty = await fake.blmove("q:src", "q:dst", timeout=0.05)
    elapsed = time.monotonic() - started
    assert empty is None
    assert elapsed >= 0.04

    async def _delayed_push() -> None:
        await asyncio.sleep(0.02)
        await fake.lpush("q:pop", "item-1")

    pusher = asyncio.create_task(_delayed_push())
    popped = await fake.blpop(["q:pop"], timeout=0.5)
    await pusher
    assert popped == ("q:pop", "item-1")

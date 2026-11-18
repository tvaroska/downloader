"""Real-Redis integration tests for WorkerQueue BLMOVE, LREM, FIFO, and reclaim contracts (R22)."""

from __future__ import annotations

import os
import uuid

import pytest
import redis.asyncio as redis

from downloader.worker_queue import WorkerQueue


@pytest.fixture
async def real_redis_client():
    redis_uri = os.getenv("REDIS_URI", "redis://localhost:6379/15")
    client = redis.from_url(redis_uri, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        pytest.fail(f"Redis is required for @pytest.mark.requires_redis tests ({redis_uri}): {exc}")

    # Clean up queue keys before and after test
    for pattern in ("queue:batch:*", "queue:browser:*", "browser_result:*", "worker:heartbeat:*"):
        keys = [k async for k in client.scan_iter(match=pattern)]
        if keys:
            await client.delete(*keys)
    try:
        yield client
    finally:
        for pattern in (
            "queue:batch:*",
            "queue:browser:*",
            "browser_result:*",
            "worker:heartbeat:*",
        ):
            keys = [k async for k in client.scan_iter(match=pattern)]
            if keys:
                await client.delete(*keys)
        await client.aclose()


@pytest.mark.integration
@pytest.mark.requires_redis
class TestWorkerQueueRealRedis:
    async def test_blmove_fifo_and_lrem_ack(self, real_redis_client: redis.Redis) -> None:
        worker_id = f"test-worker-{uuid.uuid4().hex[:6]}"
        queue = WorkerQueue(real_redis_client, worker_id=worker_id)
        try:
            await queue.enqueue_batch_job("job-a")
            await queue.enqueue_batch_job("job-b")

            env_a = await queue.pop_batch_job(timeout=1.0)
            assert env_a is not None
            assert env_a.job_id == "job-a"
            assert (await queue.get_queue_depths())["batch_processing"] == 1

            await queue.ack_batch_job(env_a.raw_payload)
            assert (await queue.get_queue_depths())["batch_processing"] == 0

            env_b = await queue.pop_batch_job(timeout=1.0)
            assert env_b is not None
            assert env_b.job_id == "job-b"
            await queue.ack_batch_job(env_b.raw_payload)
        finally:
            await queue.close()

    async def test_reclaim_respects_live_heartbeat(self, real_redis_client: redis.Redis) -> None:
        w1 = f"w1-{uuid.uuid4().hex[:6]}"
        w2 = f"w2-{uuid.uuid4().hex[:6]}"
        q1 = WorkerQueue(real_redis_client, worker_id=w1)
        q2 = WorkerQueue(real_redis_client, worker_id=w2)
        try:
            await q1.publish_heartbeat(w1, {"active_tasks": 1}, ttl=30)
            await q1.enqueue_batch_job("job-live")
            env = await q1.pop_batch_job(timeout=1.0)
            assert env is not None

            # Live heartbeat blocks reclaim by w2
            assert await q2.requeue_processing_jobs() == 0

            # Dead heartbeat allows reclaim by w2
            await q1.remove_heartbeat(w1)
            assert await q2.requeue_processing_jobs() == 1

            reclaimed = await q2.pop_batch_job(timeout=1.0)
            assert reclaimed is not None
            assert reclaimed.job_id == "job-live"
            await q2.ack_batch_job(reclaimed.raw_payload)
        finally:
            await q1.close()
            await q2.close()

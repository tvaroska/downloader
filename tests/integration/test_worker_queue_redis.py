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
            assert reclaimed.attempt == 2
            # Requeueing already-reclaimed envelope returns False without duplicating (`R113`).
            assert await q1.requeue_batch_job(env) is False
            await q2.ack_batch_job(reclaimed.raw_payload)
        finally:
            await q1.close()
            await q2.close()

    async def test_job_manager_watch_multi_and_domain_throttle_lua_on_real_redis(
        self, real_redis_client: redis.Redis
    ) -> None:
        """Exercise real-Redis WATCH/MULTI transitions, `complete_job`, and Lua throttle (`R85`, `R113`)."""
        from downloader.crawling.throttle import DomainThrottle
        from downloader.job_manager import JobManager, JobStatus

        jm = JobManager(os.getenv("REDIS_URI", "redis://localhost:6379/15"))
        jm.redis_client = real_redis_client
        try:
            job_id = await jm.create_job({"urls": [{"url": "https://example.com"}]})
            assert await jm.update_job_status(job_id, JobStatus.RUNNING) is True
            assert (
                await jm.complete_job(
                    job_id,
                    [{"url": "https://example.com", "success": True}],
                    {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
                )
                is True
            )
            res = await jm.get_job_results(job_id)
            assert res is not None and res.status == JobStatus.COMPLETED

            # Cancelled job cannot be completed
            cancel_id = await jm.create_job({"urls": [{"url": "https://example.com"}]})
            assert await jm.update_job_status(cancel_id, JobStatus.CANCELLED) is True
            assert await jm.complete_job(cancel_id, [], {}) is False
            assert await jm.get_job_results(cancel_id) is None

            throttle = DomainThrottle(max_crawl_delay=5.0, redis_client=real_redis_client)
            waited = await throttle.wait_if_needed("real-redis.example.com", 0.05)
            assert waited == 0.0
        finally:
            await real_redis_client.delete("throttle:v1:real-redis.example.com")

    async def test_remote_browser_pool_timeout_exceeds_job_manager_socket_timeout_and_maps_504(
        self, real_redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Browser pool socket_timeout stays above BLPOP timeout and maps Redis timeouts to 504 (`S-2`)."""
        from downloader.browser.renderer import RemoteBrowserRenderer
        from downloader.errors import classify_domain_error
        from downloader.job_manager import JobManager
        from downloader.ports import WorkerTimeoutError

        monkeypatch.setattr("downloader.job_manager._REDIS_SOCKET_TIMEOUT_SECONDS", 0.2)
        redis_uri = os.getenv("REDIS_URI", "redis://localhost:6379/15")
        jm = JobManager(redis_uri, max_connections=4)
        await jm.connect()
        queue = WorkerQueue(jm.redis_client)
        try:
            assert queue._browser_pool is not None
            assert float(queue._browser_pool.connection_kwargs["socket_timeout"]) > 45.0
            assert queue._browser_pool.connection_kwargs.get("retry_on_timeout") is False

            renderer = RemoteBrowserRenderer(queue, timeout=1.0)
            with pytest.raises(WorkerTimeoutError) as exc_info:
                await renderer.render_html("https://example.com/slow-remote")
            classified = classify_domain_error(exc_info.value, context="render")
            assert classified.status_code == 504
            assert classified.error_type == "worker_timeout"
        finally:
            await queue.close()
            await jm.disconnect()
            keys = [k async for k in real_redis_client.scan_iter(match="queue:browser:*")]
            if keys:
                await real_redis_client.delete(*keys)

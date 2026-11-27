"""Real-Redis integration tests for WorkerQueue BLMOVE, LREM, FIFO, and reclaim contracts (R22)."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import redis.asyncio as redis

from downloader.worker_queue import WorkerQueue


async def _noop_coro() -> None:
    return None


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
        self, real_redis_client: redis.Redis, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exercise real-Redis WATCH/MULTI races, `complete_job` re-check, and Lua throttle limit (`T-4`, `T-5`)."""
        from downloader.crawling.throttle import CrawlDelayExceededError, DomainThrottle
        from downloader.job_manager import JobManager, JobStatus

        redis_uri = os.getenv("REDIS_URI", "redis://localhost:6379/15")
        jm = JobManager(redis_uri)
        jm.redis_client = real_redis_client
        peer_client = redis.from_url(redis_uri, decode_responses=True)
        peer_jm = JobManager(redis_uri)
        peer_jm.redis_client = peer_client
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

            # Concurrent cancel landing between WATCH+GET and MULTI/EXEC on real Redis triggers WatchError -> False
            race_id = await jm.create_job({"urls": [{"url": "https://example.com/race"}]})
            assert await jm.update_job_status(race_id, JobStatus.RUNNING) is True
            orig_get = real_redis_client.get
            seen_gets = 0

            async def _racing_get(name: str):
                nonlocal seen_gets
                val = await orig_get(name)
                if name == f"job:{race_id}":
                    seen_gets += 1
                    if seen_gets == 2:
                        assert await peer_jm.update_job_status(race_id, JobStatus.CANCELLED) is True
                return val

            monkeypatch.setattr(real_redis_client, "get", _racing_get)
            assert (
                await jm.complete_job(
                    race_id,
                    [{"url": "https://example.com/race", "success": True}],
                    {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
                )
                is False
            )
            monkeypatch.setattr(real_redis_client, "get", orig_get)
            assert seen_gets >= 3
            race_info = await jm.get_job_info(race_id)
            assert race_info is not None and race_info.status == JobStatus.CANCELLED
            assert await jm.get_job_results(race_id) is None

            # Real-Redis Lua throttle: 1st = 0s, 2nd within max_wait > 0s, 3rd over max_wait -> CrawlDelayExceededError
            throttle = DomainThrottle(
                max_crawl_delay=5.0,
                max_wait_seconds=0.25,
                redis_client=real_redis_client,
            )
            monkeypatch.setattr(
                "downloader.crawling.throttle.asyncio.sleep", lambda _s: _noop_coro()
            )
            w1 = await throttle.wait_if_needed("real-redis.example.com", 0.2)
            assert w1 == 0.0
            w2 = await throttle.wait_if_needed("real-redis.example.com", 0.2)
            assert 0.0 < w2 <= 0.25
            with pytest.raises(CrawlDelayExceededError) as exc_info:
                await throttle.wait_if_needed("real-redis.example.com", 0.2)
            assert exc_info.value.retry_after > 0.25
        finally:
            await real_redis_client.delete("throttle:v1:real-redis.example.com")
            await peer_client.aclose()

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

    async def test_job_store_concurrent_complete_and_cancel_contention(
        self, real_redis_client: redis.Redis
    ) -> None:
        """`T-12`: Concurrent `complete_job`, `cancel_job`, and progress writers preserve terminal invariants."""
        from downloader.job_manager import JobManager, JobStatus

        redis_uri = os.getenv("REDIS_URI", "redis://localhost:6379/15")
        jm_a = JobManager(redis_uri)
        jm_a.redis_client = real_redis_client
        peer_client = redis.from_url(redis_uri, decode_responses=True)
        jm_b = JobManager(redis_uri)
        jm_b.redis_client = peer_client
        created_ids: list[str] = []
        try:
            for idx in range(8):
                job_id = await jm_a.create_job({"urls": [{"url": f"https://example.com/{idx}"}]})
                created_ids.append(job_id)
                assert await jm_a.update_job_status(job_id, JobStatus.RUNNING) is True
                completed, cancelled, _prog = await asyncio.gather(
                    jm_a.complete_job(
                        job_id,
                        [{"url": f"https://example.com/{idx}", "success": True}],
                        {"total_requests": 1, "successful_requests": 1, "failed_requests": 0},
                    ),
                    jm_b.cancel_job(job_id),
                    jm_a.update_job_status(job_id, JobStatus.RUNNING, progress=50),
                )
                assert (completed, cancelled) in {(True, False), (False, True)}
                info = await jm_a.get_job_info(job_id)
                results = await jm_a.get_job_results(job_id)
                assert info is not None
                if completed:
                    assert info.status == JobStatus.COMPLETED
                    assert results is not None and results.status == JobStatus.COMPLETED
                else:
                    assert info.status == JobStatus.CANCELLED
                    assert results is None
        finally:
            for job_id in created_ids:
                await real_redis_client.delete(f"job:{job_id}", f"job_result:{job_id}")
            await peer_client.aclose()

    async def test_concurrent_workers_pop_without_duplicate_or_lost_jobs(
        self, real_redis_client: redis.Redis
    ) -> None:
        """`T-12`: Multiple workers racing `BLMOVE` on real Redis deliver each job exactly once."""
        w1 = WorkerQueue(real_redis_client, worker_id=f"c1-{uuid.uuid4().hex[:6]}")
        w2 = WorkerQueue(real_redis_client, worker_id=f"c2-{uuid.uuid4().hex[:6]}")
        expected = {f"contend-{i}" for i in range(10)}
        seen: list[str] = []
        try:
            for jid in sorted(expected):
                await w1.enqueue_batch_job(jid)

            async def _drain(worker: WorkerQueue) -> None:
                while True:
                    env = await worker.pop_batch_job(timeout=0.2)
                    if env is None:
                        return
                    seen.append(env.job_id)
                    await worker.ack_batch_job(env.raw_payload)

            await asyncio.gather(_drain(w1), _drain(w2))
            assert len(seen) == len(expected)
            assert set(seen) == expected
            assert (await w1.get_queue_depths())["batch_processing"] == 0
        finally:
            await w1.close()
            await w2.close()

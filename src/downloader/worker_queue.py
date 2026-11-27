"""Redis-backed `WorkQueue` adapter for batch jobs, browser RPC, and worker heartbeats."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as redis

from .ports import (
    BatchQueueEnvelope,
    BrowserCapacityError,
    BrowserTaskRequest,
    BrowserTaskResponse,
    WorkerTimeoutError,
)
from .telemetry import inject_trace_headers

__all__ = [
    "BATCH_PENDING_QUEUE",
    "BATCH_PROCESSING_PREFIX",
    "BATCH_PROCESSING_QUEUE",
    "BROWSER_PENDING_QUEUE",
    "BROWSER_RESULT_PREFIX",
    "WORKER_HEARTBEAT_PREFIX",
    "BatchQueueEnvelope",
    "BrowserTaskRequest",
    "BrowserTaskResponse",
    "WorkerQueue",
    "WorkerTimeoutError",
]

logger = logging.getLogger(__name__)

BATCH_PENDING_QUEUE = "queue:batch:pending"
BATCH_PROCESSING_QUEUE = "queue:batch:processing"
BATCH_PROCESSING_PREFIX = "queue:batch:processing:"
BROWSER_PENDING_QUEUE = "queue:browser:pending"
BROWSER_RESULT_PREFIX = "browser_result:"
WORKER_HEARTBEAT_PREFIX = "worker:heartbeat:"

_BROWSER_RESULT_TTL_SECONDS = 120
_DEFAULT_BROWSER_SOCKET_TIMEOUT_SECONDS = 55.0

_RECLAIM_PROCESSING_LUA = """
local proc_key = KEYS[1]
local pending_key = KEYS[2]
local max_attempts = tonumber(ARGV[1])
local raw = redis.call('RPOP', proc_key)
if not raw then
    return nil
end
local ok, data = pcall(cjson.decode, raw)
if not ok or type(data) ~= 'table' or not data['job_id'] then
    return {'malformed', raw}
end
local next_attempt = (tonumber(data['attempt']) or 1) + 1
data['attempt'] = next_attempt
local updated = cjson.encode(data)
if next_attempt > max_attempts then
    return {'dead', updated}
end
redis.call('LPUSH', pending_key, updated)
return {'requeued', updated}
"""


def _parse_batch_envelope(raw: str) -> BatchQueueEnvelope | None:
    try:
        data = json.loads(raw)
        return BatchQueueEnvelope(
            job_id=str(data["job_id"]),
            trace_headers=dict(data.get("trace_headers") or {}),
            raw_payload=str(raw),
            attempt=int(data.get("attempt", 1)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


class WorkerQueue:
    """Coordinates Redis-backed queues and heartbeats between API and Worker pods."""

    def __init__(
        self,
        redis_client: Any,
        *,
        worker_id: str | None = None,
        max_remote_browser_calls: int = 8,
        max_attempts: int = 3,
        browser_redis_client: Any = None,
    ) -> None:
        self.redis = redis_client
        self.worker_id = worker_id
        self.max_attempts = max(1, max_attempts)
        self.processing_queue = (
            f"{BATCH_PROCESSING_PREFIX}{worker_id}" if worker_id else BATCH_PROCESSING_QUEUE
        )
        self._browser_inflight_semaphore = asyncio.Semaphore(max(1, max_remote_browser_calls))
        self._owns_browser_pool = False
        self._browser_pool: redis.ConnectionPool | None = None
        self._configured_browser_socket_timeout = _DEFAULT_BROWSER_SOCKET_TIMEOUT_SECONDS
        if browser_redis_client is not None:
            self._browser_redis = browser_redis_client
        elif isinstance(getattr(redis_client, "connection_pool", None), redis.ConnectionPool):
            pool = redis_client.connection_pool
            pool_kwargs = dict(pool.connection_kwargs)
            pool_kwargs["max_connections"] = max(max_remote_browser_calls + 2, 10)
            pool_kwargs["socket_timeout"] = _DEFAULT_BROWSER_SOCKET_TIMEOUT_SECONDS
            pool_kwargs["retry_on_timeout"] = False
            pool_kwargs["retry_on_error"] = [redis.BusyLoadingError, redis.ConnectionError]
            pool_kwargs.pop("retry", None)
            self._browser_pool = redis.ConnectionPool(**pool_kwargs)
            self._browser_redis = redis.Redis(connection_pool=self._browser_pool)
            self._owns_browser_pool = True
        else:
            self._browser_redis = redis_client

    async def close(self) -> None:
        """Close the dedicated browser RPC Redis pool if owned by this queue."""
        if not self._owns_browser_pool:
            return
        await self._browser_redis.close()
        if self._browser_pool is not None:
            await self._browser_pool.disconnect()

    async def enqueue_batch_job(
        self,
        job_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
        attempt: int = 1,
    ) -> None:
        payload = json.dumps(
            {
                "job_id": job_id,
                "enqueued_at": datetime.now(UTC).isoformat(),
                "attempt": attempt,
                "trace_headers": trace_headers
                if trace_headers is not None
                else inject_trace_headers(),
            },
            separators=(",", ":"),
        )
        await cast(Any, self.redis.lpush(BATCH_PENDING_QUEUE, payload))
        logger.info(f"Enqueued batch job {job_id} onto {BATCH_PENDING_QUEUE}")

    async def pop_batch_job(self, timeout: float = 2.0) -> BatchQueueEnvelope | None:
        """Atomically move the oldest queued batch job into this worker's processing list."""
        raw = await cast(
            Any,
            self.redis.blmove(
                BATCH_PENDING_QUEUE,
                self.processing_queue,
                timeout=int(max(1, round(timeout))),
                src="RIGHT",
                dest="LEFT",
            ),
        )
        if not raw:
            return None
        parsed = _parse_batch_envelope(str(raw))
        if parsed is None:
            logger.error("Malformed batch queue payload discarded")
            await self.ack_batch_job(str(raw))
            return None
        return parsed

    async def ack_batch_job(self, raw_payload: str) -> None:
        await cast(Any, self.redis.lrem(self.processing_queue, 1, raw_payload))

    async def requeue_batch_job(
        self,
        envelope: BatchQueueEnvelope,
        *,
        max_retries: int = 3,
    ) -> bool:
        """Atomically move an interrupted batch job back to pending if still owned by this worker."""
        try:
            data = json.loads(envelope.raw_payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            data = {
                "job_id": envelope.job_id,
                "enqueued_at": datetime.now(UTC).isoformat(),
                "trace_headers": envelope.trace_headers,
            }
        data["attempt"] = envelope.attempt + 1
        updated_payload = json.dumps(data, separators=(",", ":"))

        for attempt in range(max_retries):
            try:
                async with self.redis.pipeline(transaction=True) as pipe:
                    await pipe.watch(self.processing_queue)
                    entries = await cast(Any, self.redis.lrange(self.processing_queue, 0, -1))
                    if envelope.raw_payload not in entries:
                        logger.info(
                            f"Skipping requeue for job {envelope.job_id}: "
                            "entry already reclaimed from processing queue"
                        )
                        return False
                    pipe.multi()
                    pipe.lrem(self.processing_queue, 1, envelope.raw_payload)
                    pipe.lpush(BATCH_PENDING_QUEUE, updated_payload)
                    await pipe.execute()
                    logger.info(
                        f"Requeued batch job {envelope.job_id} "
                        f"(attempt={data['attempt']}) to {BATCH_PENDING_QUEUE}"
                    )
                    return True
            except redis.WatchError:
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.01 * (2**attempt))
                    continue
                raise
        return False

    async def _discover_processing_queues(self) -> list[str]:
        keys: set[str] = {BATCH_PROCESSING_QUEUE, self.processing_queue}
        cursor = 0
        while True:
            next_cursor, batch = await self.redis.scan(
                cursor=cursor, match=f"{BATCH_PROCESSING_PREFIX}*", count=100
            )
            cursor = int(next_cursor)
            keys.update(str(k) for k in cast(list[str], batch))
            if cursor == 0:
                break
        return sorted(keys)

    async def _reclaim_processing_list(
        self,
        proc_key: str,
        *,
        max_attempts: int,
        on_dead_letter: Callable[[BatchQueueEnvelope], Awaitable[None]] | None,
    ) -> int:
        moved = 0
        while True:
            result = await cast(
                Any,
                self.redis.eval(
                    _RECLAIM_PROCESSING_LUA,
                    2,
                    proc_key,
                    BATCH_PENDING_QUEUE,
                    str(max_attempts),
                ),
            )
            if not result:
                break
            outcome, payload = result[0], str(result[1])
            if isinstance(outcome, bytes):
                outcome = outcome.decode("utf-8")
            if outcome == "requeued":
                moved += 1
            elif outcome == "dead":
                envelope = _parse_batch_envelope(payload)
                if envelope is not None:
                    logger.error(
                        f"Batch job {envelope.job_id} exceeded max attempts ({max_attempts}); "
                        "dead-lettering"
                    )
                    if on_dead_letter is not None:
                        await on_dead_letter(envelope)
            else:
                logger.warning(f"Discarded malformed entry during reclaim from {proc_key}")
        return moved

    async def requeue_processing_jobs(
        self,
        *,
        max_attempts: int | None = None,
        on_dead_letter: Callable[[BatchQueueEnvelope], Awaitable[None]] | None = None,
        include_self: bool = True,
    ) -> int:
        """Atomically reclaim orphaned batch entries from dead workers with attempt tracking."""
        limit = max(1, max_attempts if max_attempts is not None else self.max_attempts)
        moved = 0
        for proc_key in await self._discover_processing_queues():
            if not include_self and proc_key == self.processing_queue:
                continue
            if proc_key.startswith(BATCH_PROCESSING_PREFIX):
                owner_id = proc_key.removeprefix(BATCH_PROCESSING_PREFIX)
                if (
                    owner_id
                    and (not include_self or owner_id != self.worker_id)
                    and await self.has_heartbeat(owner_id)
                ):
                    continue
            moved += await self._reclaim_processing_list(
                proc_key,
                max_attempts=limit,
                on_dead_letter=on_dead_letter,
            )
        if moved:
            logger.warning(f"Requeued {moved} orphaned batch job(s) back to {BATCH_PENDING_QUEUE}")
        return moved

    async def _ensure_browser_socket_timeout(self, timeout: float) -> None:
        if not self._owns_browser_pool or self._browser_pool is None:
            return
        required = float(timeout) + 5.0
        if required <= self._configured_browser_socket_timeout:
            return
        self._configured_browser_socket_timeout = required
        self._browser_pool.connection_kwargs["socket_timeout"] = required
        for conn in (
            *getattr(self._browser_pool, "_available_connections", ()),
            *getattr(self._browser_pool, "_in_use_connections", ()),
        ):
            conn.socket_timeout = required

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse:
        if self._browser_inflight_semaphore.locked():
            raise BrowserCapacityError(
                "Remote browser rendering queue is at capacity. Please try again later."
            )

        async with self._browser_inflight_semaphore:
            await self._ensure_browser_socket_timeout(timeout)
            updates: dict[str, Any] = {}
            if not task.trace_headers:
                updates["trace_headers"] = inject_trace_headers()
            if task.deadline is None:
                updates["deadline"] = time.time() + timeout
            if updates:
                task = task.model_copy(update=updates)

            result_key = f"{BROWSER_RESULT_PREFIX}{task.task_id}"
            try:
                await cast(Any, self.redis.lpush(BROWSER_PENDING_QUEUE, task.model_dump_json()))
                raw = await cast(Any, self._browser_redis.blpop([result_key], timeout=timeout))
            except (redis.TimeoutError, TimeoutError) as exc:
                raise WorkerTimeoutError(
                    f"Remote browser worker timed out after {timeout:.1f}s for task {task.task_id}"
                ) from exc
            if not raw:
                raise WorkerTimeoutError(
                    f"Remote browser worker timed out after {timeout:.1f}s for task {task.task_id}"
                )
            _, payload = raw
            return BrowserTaskResponse.model_validate_json(payload)

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None:
        raw = await cast(Any, self._browser_redis.brpop([BROWSER_PENDING_QUEUE], timeout=timeout))
        if not raw:
            return None
        _, payload = raw
        return BrowserTaskRequest.model_validate_json(payload)

    async def publish_browser_result(self, response: BrowserTaskResponse) -> None:
        result_key = f"{BROWSER_RESULT_PREFIX}{response.task_id}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(result_key, response.model_dump_json())
            pipe.expire(result_key, _BROWSER_RESULT_TTL_SECONDS)
            await pipe.execute()

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 60,
    ) -> None:
        key = f"{WORKER_HEARTBEAT_PREFIX}{worker_id}"
        payload = {
            "worker_id": worker_id,
            "updated_at": datetime.now(UTC).isoformat(),
            **metadata,
        }
        await self.redis.setex(key, ttl, json.dumps(payload, separators=(",", ":")))

    async def remove_heartbeat(self, worker_id: str) -> None:
        await self.redis.delete(f"{WORKER_HEARTBEAT_PREFIX}{worker_id}")

    async def has_heartbeat(self, worker_id: str) -> bool:
        return (await self.redis.get(f"{WORKER_HEARTBEAT_PREFIX}{worker_id}")) is not None

    async def get_queue_depths(self) -> dict[str, int]:
        processing_keys = await self._discover_processing_queues()
        batch_pending, browser_pending, *processing_counts = await asyncio.gather(
            cast(Any, self.redis.llen(BATCH_PENDING_QUEUE)),
            cast(Any, self.redis.llen(BROWSER_PENDING_QUEUE)),
            *[cast(Any, self.redis.llen(key)) for key in processing_keys],
        )
        return {
            "batch_pending": int(batch_pending),
            "batch_processing": sum(int(count) for count in processing_counts),
            "browser_pending": int(browser_pending),
        }

"""Redis-backed `WorkQueue` adapter for batch jobs, browser RPC, and worker heartbeats."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import redis.asyncio as redis
from pydantic import BaseModel, Field

from .telemetry import inject_trace_headers

logger = logging.getLogger(__name__)

BATCH_PENDING_QUEUE = "queue:batch:pending"
BATCH_PROCESSING_QUEUE = "queue:batch:processing"
BATCH_PROCESSING_PREFIX = "queue:batch:processing:"
BROWSER_PENDING_QUEUE = "queue:browser:pending"
BROWSER_RESULT_PREFIX = "browser_result:"
WORKER_HEARTBEAT_PREFIX = "worker:heartbeat:"


@dataclass(frozen=True)
class BatchQueueEnvelope:
    """Dequeued batch job reference along with its raw Redis list entry for acking."""

    job_id: str
    trace_headers: dict[str, str]
    raw_payload: str
    attempt: int = 1


class BrowserTaskRequest(BaseModel):
    """Serializable browser task delegated from stateless API pods to worker pods."""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    kind: Literal["render_html", "pdf", "screenshot"]
    url: str
    wait_for_selector: str | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)
    full_page: bool = False
    screenshot_format: Literal["png", "jpeg"] = "png"
    trace_headers: dict[str, str] = Field(default_factory=dict)
    deadline: float | None = None

    def is_expired(self, now: float | None = None) -> bool:
        """Return True when the task carries a deadline that has already passed."""
        if self.deadline is None:
            return False
        current = time.time() if now is None else now
        return current > self.deadline


class BrowserTaskResponse(BaseModel):
    """Result payload returned from a worker pod over Redis."""

    task_id: str
    success: bool
    content_base64: str | None = None
    error: str | None = None
    error_type: str | None = None
    status_code: int | None = None

    def decode_bytes(self) -> bytes:
        if not self.content_base64:
            return b""
        return base64.b64decode(self.content_base64)


class WorkerQueue:
    """Coordinates Redis-backed queues and heartbeats between API and Worker pods."""

    def __init__(
        self,
        redis_client: redis.Redis,
        *,
        worker_id: str | None = None,
        max_remote_browser_calls: int = 8,
        browser_redis_client: redis.Redis | None = None,
    ) -> None:
        self.redis = redis_client
        self.worker_id = worker_id
        self.processing_queue = (
            f"{BATCH_PROCESSING_PREFIX}{worker_id}" if worker_id else BATCH_PROCESSING_QUEUE
        )
        self._browser_inflight_semaphore = asyncio.Semaphore(max(1, max_remote_browser_calls))
        self._owns_browser_pool = False
        self._browser_pool: redis.ConnectionPool | None = None
        if browser_redis_client is not None:
            self._browser_redis = browser_redis_client
        elif isinstance(getattr(redis_client, "connection_pool", None), redis.ConnectionPool):
            pool = redis_client.connection_pool
            pool_kwargs = dict(pool.connection_kwargs)
            pool_kwargs["max_connections"] = max(max_remote_browser_calls + 2, 10)
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
        try:
            data = json.loads(raw)
            return BatchQueueEnvelope(
                job_id=str(data["job_id"]),
                trace_headers=dict(data.get("trace_headers") or {}),
                raw_payload=str(raw),
                attempt=int(data.get("attempt", 1)),
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error(f"Malformed batch queue payload discarded: {exc}")
            await self.ack_batch_job(str(raw))
            return None

    async def ack_batch_job(self, raw_payload: str) -> None:
        await cast(Any, self.redis.lrem(self.processing_queue, 1, raw_payload))

    async def requeue_batch_job(self, envelope: BatchQueueEnvelope) -> None:
        """Return an interrupted batch job to pending with an incremented attempt counter."""
        await self.ack_batch_job(envelope.raw_payload)
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
        await cast(Any, self.redis.lpush(BATCH_PENDING_QUEUE, updated_payload))
        logger.info(
            f"Requeued batch job {envelope.job_id} (attempt={data['attempt']}) to {BATCH_PENDING_QUEUE}"
        )

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
        fake_lists = getattr(self.redis, "lists", None)
        if isinstance(fake_lists, dict):
            keys.update(k for k in fake_lists if k.startswith(BATCH_PROCESSING_PREFIX))
        return sorted(keys)

    async def _drain_processing_list(self, proc_key: str) -> int:
        moved = 0
        while True:
            item = await cast(
                Any,
                self.redis.lmove(
                    proc_key,
                    BATCH_PENDING_QUEUE,
                    src="RIGHT",
                    dest="LEFT",
                ),
            )
            if not item:
                break
            moved += 1
        return moved

    async def requeue_processing_jobs(self) -> int:
        """Reclaim orphaned batch entries only from dead workers or legacy processing lists."""
        moved = 0
        for proc_key in await self._discover_processing_queues():
            if proc_key.startswith(BATCH_PROCESSING_PREFIX):
                owner_id = proc_key.removeprefix(BATCH_PROCESSING_PREFIX)
                if owner_id and owner_id != self.worker_id and await self.has_heartbeat(owner_id):
                    continue
            moved += await self._drain_processing_list(proc_key)
        if moved:
            logger.warning(f"Requeued {moved} orphaned batch job(s) back to {BATCH_PENDING_QUEUE}")
        return moved

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse:
        if self._browser_inflight_semaphore.locked():
            from .ports import BrowserCapacityError

            raise BrowserCapacityError(
                "Remote browser rendering queue is at capacity. Please try again later."
            )

        async with self._browser_inflight_semaphore:
            updates: dict[str, Any] = {}
            if not task.trace_headers:
                updates["trace_headers"] = inject_trace_headers()
            if task.deadline is None:
                updates["deadline"] = time.time() + timeout
            if updates:
                task = task.model_copy(update=updates)

            result_key = f"{BROWSER_RESULT_PREFIX}{task.task_id}"
            await cast(Any, self.redis.lpush(BROWSER_PENDING_QUEUE, task.model_dump_json()))
            raw = await cast(Any, self._browser_redis.blpop([result_key], timeout=timeout))
            if not raw:
                raise TimeoutError(
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

    async def publish_browser_result(
        self,
        response: BrowserTaskResponse,
        *,
        ttl_seconds: int = 120,
    ) -> None:
        result_key = f"{BROWSER_RESULT_PREFIX}{response.task_id}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(result_key, response.model_dump_json())
            pipe.expire(result_key, ttl_seconds)
            await pipe.execute()

    async def publish_heartbeat(
        self,
        worker_id: str,
        metadata: dict[str, Any],
        *,
        ttl: int = 30,
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

    async def list_active_workers(self) -> list[dict[str, Any]]:
        keys: list[str] = []
        cursor: int = 0
        while True:
            next_cursor, batch = await self.redis.scan(
                cursor=cursor, match=f"{WORKER_HEARTBEAT_PREFIX}*", count=100
            )
            cursor = int(next_cursor)
            keys.extend(cast(list[str], batch))
            if cursor == 0:
                break

        if not keys:
            return []

        raw_values = await self.redis.mget(keys)
        workers: list[dict[str, Any]] = []
        for raw in raw_values:
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    workers.append(parsed)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        return workers

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

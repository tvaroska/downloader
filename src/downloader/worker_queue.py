"""Redis-backed reliable job queue, remote browser RPC, and worker heartbeat registry."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
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
BROWSER_PENDING_QUEUE = "queue:browser:pending"
BROWSER_RESULT_PREFIX = "browser_result:"
WORKER_HEARTBEAT_PREFIX = "worker:heartbeat:"


@dataclass(frozen=True)
class BatchQueueEnvelope:
    """Dequeued batch job reference along with its raw Redis list entry for acking."""

    job_id: str
    trace_headers: dict[str, str]
    raw_payload: str


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

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    async def enqueue_batch_job(
        self,
        job_id: str,
        *,
        trace_headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(
            {
                "job_id": job_id,
                "enqueued_at": datetime.now(UTC).isoformat(),
                "trace_headers": trace_headers
                if trace_headers is not None
                else inject_trace_headers(),
            },
            separators=(",", ":"),
        )
        await self.redis.lpush(BATCH_PENDING_QUEUE, payload)  # type: ignore[misc]
        logger.info(f"Enqueued batch job {job_id} onto {BATCH_PENDING_QUEUE}")

    async def pop_batch_job(self, timeout: float = 2.0) -> BatchQueueEnvelope | None:
        """Atomically move the oldest queued batch job into the processing list."""
        raw = await self.redis.blmove(  # type: ignore[misc]
            BATCH_PENDING_QUEUE,
            BATCH_PROCESSING_QUEUE,
            timeout=int(max(1, round(timeout))),
            src="RIGHT",
            dest="LEFT",
        )
        if not raw:
            return None
        try:
            data = json.loads(raw)
            return BatchQueueEnvelope(
                job_id=str(data["job_id"]),
                trace_headers=dict(data.get("trace_headers") or {}),
                raw_payload=str(raw),
            )
        except Exception as exc:
            logger.error(f"Malformed batch queue payload discarded: {exc}")
            await self.ack_batch_job(str(raw))
            return None

    async def ack_batch_job(self, raw_payload: str) -> None:
        await self.redis.lrem(BATCH_PROCESSING_QUEUE, 1, raw_payload)  # type: ignore[misc]

    async def requeue_processing_jobs(self) -> int:
        """Requeue any orphaned batch entries left in processing after a crash."""
        moved = 0
        while True:
            item = await self.redis.lmove(  # type: ignore[misc]
                BATCH_PROCESSING_QUEUE,
                BATCH_PENDING_QUEUE,
                src="RIGHT",
                dest="LEFT",
            )
            if not item:
                break
            moved += 1
        if moved:
            logger.warning(f"Requeued {moved} orphaned batch job(s) back to {BATCH_PENDING_QUEUE}")
        return moved

    async def enqueue_browser_task(
        self,
        task: BrowserTaskRequest,
        *,
        timeout: float = 45.0,
    ) -> BrowserTaskResponse:
        if not task.trace_headers:
            task = task.model_copy(update={"trace_headers": inject_trace_headers()})
        result_key = f"{BROWSER_RESULT_PREFIX}{task.task_id}"
        await self.redis.lpush(BROWSER_PENDING_QUEUE, task.model_dump_json())  # type: ignore[misc]
        raw = await self.redis.blpop([result_key], timeout=timeout)  # type: ignore[misc]
        if not raw:
            raise TimeoutError(
                f"Remote browser worker timed out after {timeout:.1f}s for task {task.task_id}"
            )
        _, payload = raw
        return BrowserTaskResponse.model_validate_json(payload)

    async def pop_browser_task(self, timeout: float = 2.0) -> BrowserTaskRequest | None:
        raw = await self.redis.brpop([BROWSER_PENDING_QUEUE], timeout=timeout)  # type: ignore[misc]
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
                workers.append(json.loads(raw))
            except Exception:
                continue
        return workers

    async def get_queue_depths(self) -> dict[str, int]:
        batch_pending, batch_processing, browser_pending = await asyncio.gather(
            cast(Any, self.redis.llen(BATCH_PENDING_QUEUE)),
            cast(Any, self.redis.llen(BATCH_PROCESSING_QUEUE)),
            cast(Any, self.redis.llen(BROWSER_PENDING_QUEUE)),
        )
        return {
            "batch_pending": int(batch_pending),
            "batch_processing": int(batch_processing),
            "browser_pending": int(browser_pending),
        }

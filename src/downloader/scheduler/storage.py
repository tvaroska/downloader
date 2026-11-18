"""Redis storage for scheduled job execution history."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from ..models.schedule import ScheduleExecution

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = logging.getLogger(__name__)

DEFAULT_EXECUTION_TTL = 86400


class ExecutionStorage:
    """Redis storage for scheduled job execution history with TTL expiration."""

    def __init__(self, redis_client: redis.Redis, ttl: int = DEFAULT_EXECUTION_TTL) -> None:
        self.redis = redis_client
        self.ttl = ttl

    def _get_execution_key(self, schedule_id: str, execution_id: str) -> str:
        return f"schedule:execution:{schedule_id}:{execution_id}"

    def _get_executions_list_key(self, schedule_id: str) -> str:
        return f"schedule:executions:{schedule_id}"

    async def _trim_expired(self, list_key: str) -> None:
        cutoff = time.time() - self.ttl
        await cast(Any, self.redis.zremrangebyscore(list_key, "-inf", cutoff))

    async def store_execution(self, execution: ScheduleExecution) -> None:
        execution_key = self._get_execution_key(execution.schedule_id, execution.execution_id)
        await self.redis.setex(execution_key, self.ttl, execution.model_dump_json())

        list_key = self._get_executions_list_key(execution.schedule_id)
        await cast(
            Any,
            self.redis.zadd(list_key, {execution.execution_id: execution.started_at.timestamp()}),
        )
        await self._trim_expired(list_key)
        await self.redis.expire(list_key, self.ttl)

    async def get_executions(
        self,
        schedule_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[ScheduleExecution]:
        list_key = self._get_executions_list_key(schedule_id)
        await self._trim_expired(list_key)
        execution_ids = await cast(
            Any,
            self.redis.zrevrange(list_key, offset, offset + limit - 1),
        )
        if not execution_ids:
            return []

        executions: list[ScheduleExecution] = []
        for raw_id in execution_ids:
            exec_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            data = await self.redis.get(self._get_execution_key(schedule_id, exec_id))
            if data:
                executions.append(ScheduleExecution.model_validate_json(data))
        return executions

    async def get_execution_count(self, schedule_id: str) -> int:
        list_key = self._get_executions_list_key(schedule_id)
        await self._trim_expired(list_key)
        count = await cast(Any, self.redis.zcard(list_key))
        return int(count or 0)

    async def delete_executions(self, schedule_id: str) -> int:
        list_key = self._get_executions_list_key(schedule_id)
        execution_ids = await cast(Any, self.redis.zrange(list_key, 0, -1))
        if not execution_ids:
            return 0

        keys_to_delete = [list_key]
        for raw_id in execution_ids:
            exec_id = raw_id.decode("utf-8") if isinstance(raw_id, bytes) else str(raw_id)
            keys_to_delete.append(self._get_execution_key(schedule_id, exec_id))

        deleted = await self.redis.delete(*keys_to_delete)
        logger.info(f"Deleted {deleted} execution records for schedule {schedule_id}")
        return int(deleted)

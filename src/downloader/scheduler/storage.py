"""Redis storage for scheduled job execution history.

This module provides persistent storage for execution records using Redis,
with automatic TTL-based expiration for cleanup.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ..models.schedule import ScheduleExecution

if TYPE_CHECKING:
    import redis.asyncio as redis

logger = logging.getLogger(__name__)

# Default TTL for execution records (24 hours)
DEFAULT_EXECUTION_TTL = 86400


class ExecutionStorage:
    """Redis storage for scheduled job execution history.

    Stores execution records with automatic TTL-based expiration.
    Each schedule maintains a sorted set of execution IDs for efficient
    retrieval of recent executions.

    Key patterns:
    - schedule:execution:{schedule_id}:{execution_id} - Individual execution record
    - schedule:executions:{schedule_id} - Sorted set of execution IDs by timestamp
    """

    def __init__(self, redis_client: redis.Redis, ttl: int = DEFAULT_EXECUTION_TTL) -> None:
        """Initialize execution storage.

        Args:
            redis_client: Async Redis client instance.
            ttl: Time-to-live for execution records in seconds (default: 24 hours).
        """
        self.redis = redis_client
        self.ttl = ttl

    def _get_execution_key(self, schedule_id: str, execution_id: str) -> str:
        """Get Redis key for a specific execution record."""
        return f"schedule:execution:{schedule_id}:{execution_id}"

    def _get_executions_list_key(self, schedule_id: str) -> str:
        """Get Redis key for a schedule's execution list (sorted set)."""
        return f"schedule:executions:{schedule_id}"

    async def store_execution(self, execution: ScheduleExecution) -> None:
        """Store an execution record with TTL.

        The execution is stored as JSON with automatic expiration.
        It's also added to the schedule's sorted set for listing.

        Args:
            execution: The execution record to store.
        """
        # Store the execution record with TTL
        execution_key = self._get_execution_key(execution.schedule_id, execution.execution_id)
        await self.redis.setex(
            execution_key,
            self.ttl,
            execution.model_dump_json(),
        )

        # Add to schedule's execution list (sorted set by timestamp)
        list_key = self._get_executions_list_key(execution.schedule_id)
        await self.redis.zadd(
            list_key,
            {execution.execution_id: execution.started_at.timestamp()},
        )
        # Refresh TTL on the sorted set
        await self.redis.expire(list_key, self.ttl)

        logger.debug(
            f"Stored execution {execution.execution_id} for schedule {execution.schedule_id}"
        )

    async def get_execution(self, schedule_id: str, execution_id: str) -> ScheduleExecution | None:
        """Get a specific execution record.

        Args:
            schedule_id: The schedule identifier.
            execution_id: The execution identifier.

        Returns:
            The execution record if found, None otherwise.
        """
        key = self._get_execution_key(schedule_id, execution_id)
        data = await self.redis.get(key)
        if data is None:
            return None
        return ScheduleExecution.model_validate_json(data)

    async def get_executions(
        self, schedule_id: str, limit: int = 20, offset: int = 0
    ) -> list[ScheduleExecution]:
        """Get recent executions for a schedule (newest first).

        Args:
            schedule_id: The schedule identifier.
            limit: Maximum number of executions to return (default: 20).
            offset: Number of executions to skip (default: 0).

        Returns:
            List of execution records, ordered by start time (newest first).
        """
        list_key = self._get_executions_list_key(schedule_id)

        # Get execution IDs from sorted set (newest first via ZREVRANGE)
        start = offset
        end = offset + limit - 1
        execution_ids = await self.redis.zrevrange(list_key, start, end)

        if not execution_ids:
            return []

        # Fetch execution records
        executions: list[ScheduleExecution] = []
        for exec_id in execution_ids:
            # Handle both bytes and string from Redis
            if isinstance(exec_id, bytes):
                exec_id = exec_id.decode("utf-8")
            key = self._get_execution_key(schedule_id, exec_id)
            data = await self.redis.get(key)
            if data:
                executions.append(ScheduleExecution.model_validate_json(data))

        return executions

    async def get_execution_count(self, schedule_id: str) -> int:
        """Get the total number of stored executions for a schedule.

        Args:
            schedule_id: The schedule identifier.

        Returns:
            Number of execution records.
        """
        list_key = self._get_executions_list_key(schedule_id)
        count = await self.redis.zcard(list_key)
        return count or 0

    async def delete_executions(self, schedule_id: str) -> int:
        """Delete all execution records for a schedule.

        This is useful when deleting a schedule to clean up history.

        Args:
            schedule_id: The schedule identifier.

        Returns:
            Number of execution records deleted.
        """
        list_key = self._get_executions_list_key(schedule_id)

        # Get all execution IDs
        execution_ids = await self.redis.zrange(list_key, 0, -1)

        if not execution_ids:
            return 0

        # Delete all execution records
        keys_to_delete = [list_key]
        for exec_id in execution_ids:
            if isinstance(exec_id, bytes):
                exec_id = exec_id.decode("utf-8")
            keys_to_delete.append(self._get_execution_key(schedule_id, exec_id))

        deleted = await self.redis.delete(*keys_to_delete)

        logger.info(f"Deleted {deleted} execution records for schedule {schedule_id}")
        return deleted

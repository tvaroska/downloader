"""Mock fixtures for external dependencies."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis.asyncio as redis

from downloader.dependencies import (
    get_execution_storage_dependency,
    get_http_client,
    get_job_manager_dependency,
    get_scheduler_dependency,
)
from downloader.main import app
from downloader.models.responses import ResponseMetadata
from downloader.models.schedule import ExecutionStatus, ScheduleExecution


class FakeRedis:
    """Unified in-memory async Redis fake for robots, throttle, schedules, and worker queue tests."""

    def __init__(self, *, decode_responses: bool = False) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.lists: dict[str, list[str]] = {}
        self.versions: dict[str, int] = {}
        self.decode_responses = decode_responses
        self.fail_get: bool = False
        self.fail_set: bool = False

    def _bump_version(self, key: str) -> None:
        self.versions[key] = self.versions.get(key, 0) + 1

    async def ping(self) -> bool:
        if self.fail_get or self.fail_set:
            raise redis.ConnectionError("simulated redis ping failure")
        return True

    async def get(self, key: str) -> Any:
        if self.fail_get:
            raise redis.ConnectionError("simulated redis read failure")
        entry = self.store.get(key)
        if entry is None:
            return None
        return entry[0] if self.decode_responses else entry[0].encode("utf-8")

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
    ) -> bool:
        if self.fail_set:
            raise redis.ConnectionError("simulated redis write failure")
        if nx and key in self.store:
            return False
        ttl_s = float(ex) if ex is not None else (float(px) / 1000.0 if px is not None else None)
        self.store[key] = (value, ttl_s)
        self._bump_version(key)
        return True

    async def psetex(self, key: str, time_ms: int, value: str) -> bool:
        if self.fail_set:
            raise redis.ConnectionError("simulated redis psetex failure")
        self.store[key] = (value, float(time_ms) / 1000.0)
        self._bump_version(key)
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        if self.fail_set:
            raise redis.ConnectionError("simulated redis setex failure")
        self.store[key] = (value, float(ttl))
        self._bump_version(key)
        return True

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            removed_kv = self.store.pop(key, None) is not None
            removed_list = self.lists.pop(key, None) is not None
            if removed_kv or removed_list:
                self._bump_version(key)
                deleted += 1
        return deleted

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 100
    ) -> tuple[int, list[str]]:
        del cursor, count
        prefix = match.rstrip("*")
        keys = sorted(
            {
                *(k for k in self.store if k.startswith(prefix)),
                *(k for k, vals in self.lists.items() if vals and k.startswith(prefix)),
            }
        )
        return 0, keys

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store[k][0] if k in self.store else None for k in keys]

    def _eval_release_lock(self, lock_key: str, token: str) -> int:
        entry = self.store.get(lock_key)
        if entry is not None and entry[0] == token:
            self.store.pop(lock_key, None)
            self._bump_version(lock_key)
            return 1
        return 0

    def _eval_reclaim_batch(self, proc_key: str, pending_key: str, max_attempts: int) -> Any:
        bucket = self.lists.get(proc_key) or []
        if not bucket:
            return None
        raw = bucket.pop()
        self._bump_version(proc_key)
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or not data.get("job_id"):
                return ["malformed", raw]
        except (json.JSONDecodeError, TypeError, ValueError):
            return ["malformed", raw]
        next_attempt = int(data.get("attempt", 1)) + 1
        data["attempt"] = next_attempt
        updated = json.dumps(data, separators=(",", ":"))
        if next_attempt > max_attempts:
            return ["dead", updated]
        self.lists.setdefault(pending_key, []).insert(0, updated)
        self._bump_version(pending_key)
        return ["requeued", updated]

    def _eval_refund_slot(self, key: str, args: list[str]) -> int:
        expected_next, delay, now_wall = float(args[0]), float(args[1]), float(args[2])
        entry = self.store.get(key)
        if entry is None or abs(float(entry[0]) - expected_next) > 0.001:
            return 0
        refunded = float(entry[0]) - delay
        if refunded <= now_wall:
            self.store.pop(key, None)
        else:
            ttl_ms = max(1, int((refunded - now_wall) * 1000 + 0.5))
            self.store[key] = (str(refunded), ttl_ms / 1000.0)
        self._bump_version(key)
        return 1

    def _eval_reserve_slot(self, key: str, args: list[str]) -> str:
        now_wall, delay = float(args[0]), float(args[1])
        max_wait = float(args[2]) if len(args) > 2 and args[2] is not None else None
        entry = self.store.get(key)
        next_wall = float(entry[0]) if entry is not None else 0.0
        if now_wall < next_wall:
            wait_s = next_wall - now_wall
            if max_wait is not None and wait_s > max_wait:
                return str(-wait_s)
            new_next = next_wall + delay
        else:
            wait_s = 0.0
            new_next = now_wall + delay
        ttl_ms = max(1, int((new_next - now_wall) * 1000 + 0.5))
        self.store[key] = (str(new_next), ttl_ms / 1000.0)
        self._bump_version(key)
        return str(wait_s)

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: Any,
    ) -> Any:
        if self.fail_get or self.fail_set:
            raise redis.ConnectionError("Redis eval failed")

        keys = [str(k) for k in keys_and_args[:numkeys]]
        args = [str(a) for a in keys_and_args[numkeys:]]

        if "redis.call('get', KEYS[1]) == ARGV[1]" in script:
            return self._eval_release_lock(keys[0], args[0])
        if "RPOP" in script and "cjson.decode" in script:
            return self._eval_reclaim_batch(keys[0], keys[1], int(args[0]))
        if "expected_next" in script and "refunded" in script:
            return self._eval_refund_slot(keys[0], args)
        return self._eval_reserve_slot(keys[0], args)

    async def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        self._bump_version(key)
        return len(bucket)

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        self._bump_version(key)
        return len(bucket)

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        bucket = self.lists.get(key) or []
        if end < 0:
            end_idx = len(bucket) + end + 1
        else:
            end_idx = end + 1
        return list(bucket[start:end_idx])

    async def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: int = 0,
        src: str = "RIGHT",
        dest: str = "LEFT",
    ) -> str | None:
        del timeout
        src_bucket = self.lists.get(first_list) or []
        if not src_bucket:
            return None
        item = src_bucket.pop() if src == "RIGHT" else src_bucket.pop(0)
        self._bump_version(first_list)
        dst_bucket = self.lists.setdefault(second_list, [])
        if dest == "LEFT":
            dst_bucket.insert(0, item)
        else:
            dst_bucket.append(item)
        self._bump_version(second_list)
        return item

    async def lmove(
        self,
        first_list: str,
        second_list: str,
        src: str = "RIGHT",
        dest: str = "LEFT",
    ) -> str | None:
        return await self.blmove(first_list, second_list, timeout=0, src=src, dest=dest)

    async def lrem(self, key: str, count: int, value: str) -> int:
        bucket = self.lists.get(key) or []
        removed = 0
        while value in bucket and (count == 0 or removed < count):
            bucket.remove(value)
            removed += 1
        if removed:
            self._bump_version(key)
        return removed

    async def blpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        del timeout
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                item = bucket.pop(0)
                self._bump_version(key)
                return key, item
        return None

    async def brpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        del timeout
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                item = bucket.pop()
                self._bump_version(key)
                return key, item
        return None

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    def pipeline(self, transaction: bool = True) -> Any:
        return _FakeRedisPipeline(self, transaction=transaction)


class _FakeRedisPipeline:
    """Transactional async Redis pipeline double enforcing `WATCH` and `MULTI` buffering (`T-4`)."""

    def __init__(self, parent: FakeRedis, *, transaction: bool = True) -> None:
        self._parent = parent
        self._transaction = transaction
        self._ops: list[tuple[str, tuple[Any, ...]]] = []
        self._watched: dict[str, int] = {}
        self._in_multi = False

    async def __aenter__(self) -> _FakeRedisPipeline:
        return self

    async def __aexit__(self, *args: object) -> None:
        self.reset()

    def reset(self) -> None:
        self._ops.clear()
        self._watched.clear()
        self._in_multi = False

    async def watch(self, *keys: str) -> None:
        if self._in_multi:
            raise redis.RedisError("Cannot call WATCH inside MULTI")
        for key in keys:
            self._watched[str(key)] = self._parent.versions.get(str(key), 0)

    def multi(self) -> None:
        self._in_multi = True

    def _queue_op(self, op: str, args: tuple[Any, ...]) -> _FakeRedisPipeline:
        if self._watched and not self._in_multi:
            raise redis.RedisError(f"Pipeline after WATCH requires multi() before queuing {op}()")
        self._ops.append((op, args))
        return self

    def setex(self, key: str, ttl: int, value: str) -> _FakeRedisPipeline:
        return self._queue_op("setex", (key, ttl, value))

    def lpush(self, key: str, value: str) -> _FakeRedisPipeline:
        return self._queue_op("lpush", (key, value))

    def rpush(self, key: str, value: str) -> _FakeRedisPipeline:
        return self._queue_op("rpush", (key, value))

    def lrem(self, key: str, count: int, value: str) -> _FakeRedisPipeline:
        return self._queue_op("lrem", (key, count, value))

    def expire(self, key: str, ttl: int) -> _FakeRedisPipeline:
        return self._queue_op("expire", (key, ttl))

    async def execute(self) -> list[Any]:
        try:
            if self._watched:
                if not self._in_multi:
                    raise redis.RedisError("Transactional pipeline after WATCH requires multi()")
                for key, expected_ver in self._watched.items():
                    if self._parent.versions.get(key, 0) != expected_ver:
                        raise redis.WatchError(f"Watched key {key!r} modified concurrently")
            results: list[Any] = []
            for op, args in self._ops:
                if op == "setex":
                    results.append(
                        await self._parent.setex(str(args[0]), int(args[1]), str(args[2]))
                    )
                elif op == "lpush":
                    results.append(await self._parent.lpush(str(args[0]), str(args[1])))
                elif op == "rpush":
                    results.append(await self._parent.rpush(str(args[0]), str(args[1])))
                elif op == "lrem":
                    results.append(
                        await self._parent.lrem(str(args[0]), int(args[1]), str(args[2]))
                    )
                else:
                    results.append(True)
            return results
        finally:
            self.reset()


class MockPipeline:
    """Mock Redis pipeline for transaction testing."""

    def __init__(self) -> None:
        self.watch = AsyncMock()
        self.multi = MagicMock()
        self.execute = AsyncMock(return_value=[True, True])
        self.setex = MagicMock()
        self.lrem = MagicMock()
        self.lpush = MagicMock()
        self.rpush = MagicMock()
        self.expire = MagicMock()

    async def __aenter__(self) -> MockPipeline:
        return self

    async def __aexit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        pass


class MockPipelineWithWatchError(MockPipeline):
    """Pipeline that raises WatchError for retry testing."""

    def __init__(self, fail_count: int = 1) -> None:
        super().__init__()
        self.fail_count = fail_count
        self.attempt = 0

        async def _execute() -> list[bool]:
            self.attempt += 1
            if self.attempt <= self.fail_count:
                raise redis.WatchError("Concurrent modification")
            return [True, True]

        self.execute = AsyncMock(side_effect=_execute)


def make_response_metadata(**overrides: Any) -> ResponseMetadata:
    """Build a complete ResponseMetadata dictionary for tests."""
    return {
        "status_code": int(overrides.get("status_code", 200)),
        "headers": dict(overrides.get("headers", {"content-type": "text/html"})),
        "url": str(overrides.get("url", "https://example.com")),
        "size": int(overrides.get("size", 17)),
        "content_type": str(overrides.get("content_type", "text/html")),
        "http_version": str(overrides.get("http_version", "HTTP/1.1")),
        "connection_reused": overrides.get("connection_reused", False),
    }


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client."""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """Fixture to mock the HTTP client with default successful response."""
    mock_client = AsyncMock()
    mock_client.download.return_value = (
        b"<html>test</html>",
        make_response_metadata(),
    )
    return mock_client


@pytest.fixture
def override_http_client(mock_http_client):
    """Fixture to override the HTTP client dependency with a mock."""

    async def mock_get_http_client():
        return mock_http_client

    app.dependency_overrides[get_http_client] = mock_get_http_client
    yield mock_http_client
    app.dependency_overrides.pop(get_http_client, None)


@pytest.fixture
def override_job_manager(mock_job_manager):
    """Fixture to override the job manager dependency with a mock."""

    async def mock_get_job_manager():
        return mock_job_manager

    app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
    yield mock_job_manager
    app.dependency_overrides.pop(get_job_manager_dependency, None)


@pytest.fixture
def mock_job_manager():
    """Fixture to mock the JobManager."""
    mock_manager = AsyncMock()
    mock_manager.create_job.return_value = "test_job_id"
    return mock_manager


@pytest.fixture
def mock_playwright():
    """Mock playwright instance for PDF generation tests."""
    with patch("downloader.browser.manager.async_playwright") as mock:
        playwright_instance = AsyncMock()
        mock.return_value.start = AsyncMock(return_value=playwright_instance)

        browsers = []

        def create_browser():
            browser = AsyncMock()
            browser.is_connected = MagicMock(return_value=True)
            browsers.append(browser)
            return browser

        first_browser = create_browser()

        async def mock_launch(*args, **kwargs):
            del args, kwargs
            if len(browsers) == 1:
                return first_browser
            return create_browser()

        playwright_instance.chromium.launch = AsyncMock(side_effect=mock_launch)
        yield mock, playwright_instance, first_browser


@pytest.fixture
def mock_browser_pool():
    """Mock browser pool for PDF generation tests."""
    with patch("downloader.pdf_generator.BrowserPool") as mock_pool_class:
        pool_instance = AsyncMock()
        mock_pool_class.return_value = pool_instance

        browser = AsyncMock()
        browser.is_connected = MagicMock(return_value=True)

        async def _delegate_create_context(br, **kwargs):
            return await br.new_context(**kwargs)

        pool_instance.get_browser = AsyncMock(return_value=browser)
        pool_instance.release_browser = AsyncMock()
        pool_instance.create_context = AsyncMock(side_effect=_delegate_create_context)
        pool_instance.available_count = 3
        yield pool_instance, browser


# ============= Scheduler Fixtures =============


@pytest.fixture
def mock_scheduler_service():
    """Mock SchedulerService for testing routes."""
    mock_svc = MagicMock()
    mock_svc.is_running.return_value = True
    mock_svc.get_jobs.return_value = []
    mock_svc.get_job.return_value = None
    mock_svc.remove_job = MagicMock()
    mock_svc._scheduler = MagicMock()
    type(mock_svc).scheduler = property(lambda self: self._scheduler)
    return mock_svc


@pytest.fixture
def mock_execution_storage():
    """Mock ExecutionStorage for testing routes and executor."""
    mock_storage = AsyncMock()
    mock_storage.store_execution = AsyncMock()
    mock_storage.get_executions = AsyncMock(return_value=[])
    mock_storage.get_execution_count = AsyncMock(return_value=0)
    mock_storage.delete_executions = AsyncMock(return_value=0)
    return mock_storage


@pytest.fixture
def mock_apscheduler_job():
    """Mock APScheduler Job object for testing."""
    job = MagicMock()
    job.id = "test-schedule-id"
    job.name = "Test Schedule"
    job.next_run_time = datetime.now(UTC) + timedelta(hours=1)
    job.kwargs = {
        "schedule_id": "test-schedule-id",
        "url": "https://example.com",
        "format": "text",
        "headers": None,
        "created_at": datetime.now(UTC).isoformat(),
    }
    job.trigger = MagicMock()
    job.trigger.fields = [
        MagicMock(name="month"),
        MagicMock(name="day"),
        MagicMock(name="day_of_week"),
        MagicMock(name="hour"),
        MagicMock(name="minute"),
    ]
    job.pause = MagicMock()
    return job


@pytest.fixture
def sample_schedule_execution():
    """Sample ScheduleExecution for testing (COMPLETED status)."""
    return ScheduleExecution(
        execution_id="exec-123",
        schedule_id="schedule-456",
        status=ExecutionStatus.COMPLETED,
        started_at=datetime.now(UTC) - timedelta(seconds=5),
        completed_at=datetime.now(UTC),
        duration=5.0,
        success=True,
        content_size=1024,
        error_message=None,
        attempt=1,
    )


@pytest.fixture
def sample_failed_execution():
    """Sample failed ScheduleExecution for testing (FAILED status)."""
    return ScheduleExecution(
        execution_id="exec-789",
        schedule_id="schedule-456",
        status=ExecutionStatus.FAILED,
        started_at=datetime.now(UTC) - timedelta(seconds=2),
        completed_at=datetime.now(UTC),
        duration=2.0,
        success=False,
        content_size=None,
        error_message="HTTPClientError: HTTP 404: Not Found",
        attempt=3,
    )


@pytest.fixture
def override_scheduler_service(mock_scheduler_service):
    """Override scheduler dependency for route testing."""

    async def mock_get_scheduler():
        return mock_scheduler_service

    app.dependency_overrides[get_scheduler_dependency] = mock_get_scheduler
    yield mock_scheduler_service
    app.dependency_overrides.pop(get_scheduler_dependency, None)


@pytest.fixture
def override_execution_storage(mock_execution_storage):
    """Override execution storage dependency for route testing."""

    async def mock_get_storage():
        return mock_execution_storage

    app.dependency_overrides[get_execution_storage_dependency] = mock_get_storage
    yield mock_execution_storage
    app.dependency_overrides.pop(get_execution_storage_dependency, None)

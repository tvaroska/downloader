"""Mock fixtures for external dependencies."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import redis.asyncio as redis

from downloader.dependencies import (
    get_execution_storage_dependency,
    get_http_client,
    get_job_manager_dependency,
    get_pdf_generator_dependency,
    get_scheduler_dependency,
)
from downloader.http_client import HTTPClient
from downloader.main import app
from downloader.models.responses import ResponseMetadata
from downloader.models.schedule import ExecutionStatus, ScheduleExecution


class FakeRedis:
    """Unified in-memory async Redis fake for robots, throttle, and worker queue tests."""

    def __init__(self) -> None:
        self.store: dict[str, tuple[str, float | None]] = {}
        self.lists: dict[str, list[str]] = {}
        self.fail_get: bool = False
        self.fail_set: bool = False

    async def get(self, key: str) -> bytes | None:
        if self.fail_get:
            raise ConnectionError("simulated redis read failure")
        entry = self.store.get(key)
        if entry is None:
            return None
        return entry[0].encode("utf-8")

    async def set(
        self,
        key: str,
        value: str,
        *,
        ex: int | None = None,
        px: int | None = None,
    ) -> bool:
        if self.fail_set:
            raise ConnectionError("simulated redis write failure")
        ttl_s = float(ex) if ex is not None else (float(px) / 1000.0 if px is not None else None)
        self.store[key] = (value, ttl_s)
        return True

    async def psetex(self, key: str, time_ms: int, value: str) -> bool:
        if self.fail_set:
            raise ConnectionError("simulated redis psetex failure")
        self.store[key] = (value, float(time_ms) / 1000.0)
        return True

    async def setex(self, key: str, ttl: int, value: str) -> bool:
        if self.fail_set:
            raise ConnectionError("simulated redis setex failure")
        self.store[key] = (value, float(ttl))
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def scan(
        self, cursor: int = 0, match: str = "*", count: int = 100
    ) -> tuple[int, list[str]]:
        prefix = match.rstrip("*")
        keys = [k for k in self.store if k.startswith(prefix)]
        return 0, keys

    async def mget(self, keys: list[str]) -> list[str | None]:
        return [self.store[k][0] if k in self.store else None for k in keys]

    async def eval(
        self,
        _script: str,
        _numkeys: int,
        key: str,
        now_str: str,
        delay_str: str,
        max_wait_str: str | None = None,
    ) -> str:
        if self.fail_get or self.fail_set:
            raise ConnectionError("Redis eval failed")
        now_wall = float(now_str)
        delay = float(delay_str)
        max_wait = float(max_wait_str) if max_wait_str is not None else None
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
        return str(wait_s)

    async def lpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.insert(0, value)
        return len(bucket)

    async def rpush(self, key: str, value: str) -> int:
        bucket = self.lists.setdefault(key, [])
        bucket.append(value)
        return len(bucket)

    async def blmove(
        self,
        first_list: str,
        second_list: str,
        timeout: int = 0,
        src: str = "RIGHT",
        dest: str = "LEFT",
    ) -> str | None:
        src_bucket = self.lists.get(first_list) or []
        if not src_bucket:
            return None
        item = src_bucket.pop() if src == "RIGHT" else src_bucket.pop(0)
        dst_bucket = self.lists.setdefault(second_list, [])
        if dest == "LEFT":
            dst_bucket.insert(0, item)
        else:
            dst_bucket.append(item)
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
        return removed

    async def blpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                return key, bucket.pop(0)
        return None

    async def brpop(self, keys: list[str], timeout: float = 0) -> tuple[str, str] | None:
        for key in keys:
            bucket = self.lists.get(key) or []
            if bucket:
                return key, bucket.pop()
        return None

    async def llen(self, key: str) -> int:
        return len(self.lists.get(key) or [])

    def pipeline(self, transaction: bool = True) -> Any:
        parent = self

        class _Pipe:
            def __init__(self) -> None:
                self._ops: list[tuple[str, tuple[object, ...]]] = []

            async def __aenter__(self) -> "_Pipe":
                return self

            async def __aexit__(self, *args: object) -> None:
                return None

            def rpush(self, key: str, value: str) -> None:
                self._ops.append(("rpush", (key, value)))

            def expire(self, key: str, ttl: int) -> None:
                self._ops.append(("expire", (key, ttl)))

            async def execute(self) -> list[bool]:
                for op, args in self._ops:
                    if op == "rpush":
                        await parent.rpush(str(args[0]), str(args[1]))
                return [True] * len(self._ops)

        return _Pipe()


class MockPipeline:
    """Mock Redis pipeline for transaction testing."""

    def __init__(self) -> None:
        self.watch = AsyncMock()
        self.multi = MagicMock()  # multi() is not async
        self.execute = AsyncMock(return_value=[True, True])
        self.setex = AsyncMock()

    async def __aenter__(self) -> "MockPipeline":
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
    metadata: ResponseMetadata = {
        "status_code": 200,
        "headers": {"content-type": "text/html"},
        "url": "https://example.com",
        "size": 17,
        "content_type": "text/html",
        "http_version": "HTTP/1.1",
        "connection_reused": False,
    }
    metadata.update(overrides)  # type: ignore[typeddict-item]
    return metadata


def attach_mock_transport(
    http_client: HTTPClient,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """Attach an httpx.MockTransport to an HTTPClient while preserving redirect hooks."""
    http_client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        max_redirects=http_client.max_redirects,
        event_hooks={"response": [http_client._on_response_hook]},
    )


@pytest.fixture
def mock_redis_client():
    """Fixture to mock the Redis client."""
    return AsyncMock()


@pytest.fixture
def mock_http_client():
    """
    Fixture to mock the HTTP client with default successful response.

    Returns a mock HTTP client that simulates a successful download
    of HTML content from example.com.
    """
    mock_client = AsyncMock()
    mock_client.download.return_value = (
        b"<html>test</html>",
        make_response_metadata(),
    )
    return mock_client


@pytest.fixture
def override_http_client(mock_http_client):
    """
    Fixture to override the HTTP client dependency with a mock.

    Use this fixture when testing endpoints that use HTTPClientDep.
    """

    async def mock_get_http_client():
        return mock_http_client

    app.dependency_overrides[get_http_client] = mock_get_http_client
    yield mock_http_client
    app.dependency_overrides.pop(get_http_client, None)


@pytest.fixture
def override_job_manager(mock_job_manager):
    """
    Fixture to override the job manager dependency with a mock.

    Use this fixture when testing batch endpoints.
    """

    async def mock_get_job_manager():
        return mock_job_manager

    app.dependency_overrides[get_job_manager_dependency] = mock_get_job_manager
    yield mock_job_manager
    app.dependency_overrides.pop(get_job_manager_dependency, None)


@pytest.fixture
def override_pdf_generator():
    """
    Fixture to override the PDF generator dependency with a mock.
    """
    mock_generator = AsyncMock()
    mock_generator.generate_pdf.return_value = b"%PDF-1.4 mock content"

    async def mock_get_pdf_generator():
        return mock_generator

    app.dependency_overrides[get_pdf_generator_dependency] = mock_get_pdf_generator
    yield mock_generator
    app.dependency_overrides.pop(get_pdf_generator_dependency, None)


@pytest.fixture
def mock_job_manager():
    """Fixture to mock the JobManager."""
    mock_manager = AsyncMock()
    mock_manager.create_job.return_value = "test_job_id"
    return mock_manager


@pytest.fixture
def mock_playwright():
    """Mock playwright instance for PDF generation tests.

    Creates unique browser mocks for each launch() call to ensure BrowserPool's
    set-based storage works correctly (the set stores unique objects).
    """
    with patch("downloader.browser.manager.async_playwright") as mock:
        playwright_instance = AsyncMock()
        mock.return_value.start = AsyncMock(return_value=playwright_instance)

        # Track all browser instances for assertions
        browsers = []

        def create_browser():
            browser = AsyncMock()
            browser.is_connected = MagicMock(return_value=True)
            browsers.append(browser)
            return browser

        # Create a wrapper that tracks the first browser for backward compatibility
        first_browser = create_browser()

        async def mock_launch(*args, **kwargs):
            if len(browsers) == 1:
                return first_browser
            return create_browser()

        playwright_instance.chromium.launch = AsyncMock(side_effect=mock_launch)

        # Yield first_browser for backward compatibility with existing tests
        yield mock, playwright_instance, first_browser


@pytest.fixture
def mock_browser_pool():
    """Mock browser pool for PDF generation tests."""
    # Patch both locations for backward compatibility
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

    # The scheduler property returns the underlying _scheduler
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
    # Simulating APScheduler CronTrigger fields (month, day, week, day_of_week, hour, minute, second)
    job.trigger.fields = [
        MagicMock(),
        "*",
        MagicMock(),
        "*",
        "*",
        "9",
        "0",
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

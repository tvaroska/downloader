"""Unit tests for SchedulerService lifecycle and ScheduledJobExecutor."""

import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from downloader.config import SchedulerConfig
from downloader.models.schedule import (
    ExecutionStatus,
    ScheduleExecution,
    cron_trigger_from_unix_crontab,
)
from downloader.scheduler.executor import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    ScheduledJobExecutor,
)
from downloader.scheduler.service import SchedulerService
from downloader.scheduler.storage import ExecutionStorage


class TestSchedulerServiceInit:
    """Tests for SchedulerService initialization."""

    def test_init_with_memory_job_store_by_config(self):
        """Test in-memory job store when configured."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri="redis://localhost:6379", settings=settings)

        assert service._scheduler is not None
        assert service._started is False

    def test_init_with_memory_job_store_when_no_redis(self):
        """Redis job store raises cleanly when `redis_uri` is missing instead of silent memory fallback (`R114`)."""
        settings = SchedulerConfig(job_store_type="redis")
        with pytest.raises(RuntimeError, match="REDIS_URI is required"):
            SchedulerService(redis_uri=None, settings=settings)

    def test_init_redis_error_propagates_without_silent_memory_fallback(self):
        """When Redis job store is explicitly configured, connection errors raise instead of silently falling back (`R114`)."""
        settings = SchedulerConfig(job_store_type="redis")

        with (
            patch(
                "downloader.scheduler.service._get_redis_job_store",
                side_effect=OSError("Connection refused"),
            ),
            pytest.raises(OSError, match="Connection refused"),
        ):
            SchedulerService(redis_uri="redis://badhost:6379", settings=settings)

    def test_init_with_custom_settings(self):
        """Test that custom settings are applied."""
        settings = SchedulerConfig(
            job_store_type="memory",
            max_workers=5,
            misfire_grace_time=120,
            coalesce=False,
        )
        service = SchedulerService(redis_uri=None, settings=settings)

        assert service.settings.max_workers == 5
        assert service.settings.misfire_grace_time == 120
        assert service.settings.coalesce is False

    def test_scheduler_property_returns_scheduler(self):
        """Test scheduler property returns the underlying scheduler."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri=None, settings=settings)

        assert service.scheduler is not None

    def test_scheduler_property_raises_when_none(self):
        """Test scheduler property raises when _scheduler is None."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri=None, settings=settings)
        service._scheduler = None

        with pytest.raises(RuntimeError, match="Scheduler not initialized"):
            _ = service.scheduler


@pytest.mark.unit
class TestSchedulerServiceLifecycle:
    """Tests for SchedulerService lifecycle management."""

    @pytest.fixture
    def service(self):
        """Create a SchedulerService with memory store."""
        settings = SchedulerConfig(job_store_type="memory")
        return SchedulerService(redis_uri=None, settings=settings)

    @pytest.mark.asyncio
    async def test_start_scheduler(self, service):
        """Test starting the scheduler."""
        await service.start()

        assert service._started is True
        assert service.is_running() is True

        # Clean up
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_start_already_started(self, service):
        """Test starting an already started scheduler logs warning."""
        await service.start()

        # Start again - should be a no-op
        await service.start()

        assert service._started is True

        # Clean up
        await service.shutdown()

    @pytest.mark.asyncio
    async def test_start_not_initialized_raises(self, service):
        """Test starting when _scheduler is None raises error."""
        service._scheduler = None

        with pytest.raises(RuntimeError, match="Scheduler not initialized"):
            await service.start()

    @pytest.mark.asyncio
    async def test_shutdown_graceful(self, service):
        """Test graceful shutdown with wait=True."""
        await service.start()
        await service.shutdown(wait=True)

        assert service._started is False

    @pytest.mark.asyncio
    async def test_shutdown_immediate(self, service):
        """Test immediate shutdown with wait=False."""
        await service.start()
        await service.shutdown(wait=False)

        assert service._started is False

    @pytest.mark.asyncio
    async def test_shutdown_not_running(self, service):
        """Test shutdown when not running is a no-op."""
        # Don't start, just shutdown
        await service.shutdown()

        assert service._started is False

    @pytest.mark.asyncio
    async def test_is_running_true_when_started(self, service):
        """Test is_running returns True after start."""
        await service.start()

        assert service.is_running() is True

        # Clean up
        await service.shutdown()

    def test_is_running_false_when_stopped(self, service):
        """Test is_running returns False before start."""
        assert service.is_running() is False

    @pytest.mark.asyncio
    async def test_job_added_by_paused_scheduler_is_run_by_active_scheduler(self):
        """Test a job added via a paused API scheduler is discovered and run by an active worker."""
        import asyncio

        from apscheduler.jobstores.memory import MemoryJobStore
        from apscheduler.triggers.date import DateTrigger

        shared_store = MemoryJobStore()
        settings = SchedulerConfig(job_store_type="memory", misfire_grace_time=60)

        active_svc = SchedulerService(redis_uri=None, settings=settings, wakeup_interval=0.05)
        paused_svc = SchedulerService(redis_uri=None, settings=settings, wakeup_interval=0.05)
        active_svc.scheduler._jobstores["default"] = shared_store
        paused_svc.scheduler._jobstores["default"] = shared_store

        ran_event = asyncio.Event()

        async def _mark_ran() -> None:
            ran_event.set()

        await active_svc.start(paused=False)
        await paused_svc.start(paused=True)
        try:
            paused_svc.scheduler.add_job(
                func=_mark_ran,
                trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(milliseconds=20)),
                id="cross-pod-job",
                replace_existing=True,
            )
            await asyncio.wait_for(ran_event.wait(), timeout=2.0)
            assert ran_event.is_set()
        finally:
            await paused_svc.shutdown(wait=False)
            await active_svc.shutdown(wait=False)


@pytest.mark.unit
class TestSchedulerServiceJobManagement:
    """Tests for SchedulerService job management methods."""

    @pytest.fixture
    def service(self):
        """Create a SchedulerService with mocked scheduler."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri=None, settings=settings)
        return service

    @pytest.mark.asyncio
    async def test_get_status_not_initialized(self, service):
        """Test get_status when scheduler is None."""
        service._scheduler = None

        status = await service.get_status()

        assert status["status"] == "not_initialized"

    @pytest.mark.asyncio
    async def test_get_status_stopped(self, service):
        """Test get_status when scheduler is not started."""
        status = await service.get_status()

        assert status["status"] == "stopped"

    @pytest.mark.asyncio
    async def test_get_status_running(self, service):
        """Test get_status when scheduler is running."""
        await service.start()

        status = await service.get_status()

        assert status["status"] == "running"
        assert "total_jobs" in status
        assert "pending_jobs" in status

        await service.shutdown()

    def test_get_jobs_returns_list(self, service):
        """Test get_jobs returns list of jobs."""
        jobs = service.get_jobs()

        assert isinstance(jobs, list)
        assert len(jobs) == 0

    def test_get_jobs_empty_when_no_scheduler(self, service):
        """Test get_jobs returns empty list when scheduler is None."""
        service._scheduler = None

        jobs = service.get_jobs()

        assert jobs == []

    def test_get_job_not_found(self, service):
        """Test get_job returns None when job doesn't exist."""
        result = service.get_job("nonexistent")

        assert result is None

    def test_get_job_no_scheduler(self, service):
        """Test get_job returns None when scheduler is None."""
        service._scheduler = None

        result = service.get_job("any-id")

        assert result is None

    def test_remove_job(self, service):
        """Test remove_job calls scheduler.remove_job."""
        with patch.object(service._scheduler, "remove_job") as mock_remove:
            service.remove_job("job-123")

            mock_remove.assert_called_once_with("job-123")

    def test_remove_job_no_scheduler(self, service):
        """Test remove_job is no-op when scheduler is None."""
        service._scheduler = None

        assert service.remove_job("job-123") is None
        assert service._scheduler is None


# ============= ScheduledJobExecutor Tests =============


@pytest.mark.unit
class TestScheduledJobExecutorConstants:
    """Tests for executor constants."""

    def test_max_attempts_value(self):
        """Test MAX_ATTEMPTS constant."""
        assert MAX_ATTEMPTS == 3

    def test_retry_delays_values(self):
        """Test RETRY_DELAYS constant."""
        assert RETRY_DELAYS == [5, 15, 30]


@pytest.mark.unit
class TestScheduledJobExecutorProcessContent:
    """Tests for ScheduledJobExecutor._process_content()."""

    @pytest.fixture
    def mock_http_client(self):
        """Create a mock HTTP client."""
        return AsyncMock()

    @pytest.fixture
    def mock_storage(self):
        """Create a mock execution storage."""
        return AsyncMock()

    @pytest.fixture
    def executor(self, mock_http_client, mock_storage):
        """Create a ScheduledJobExecutor with mocks and a stub BrowserRenderer (`R114`)."""
        from unittest.mock import MagicMock

        from downloader.ports import BrowserRenderer

        stub_renderer = MagicMock(spec=BrowserRenderer)
        stub_renderer.render_html = AsyncMock(return_value=None)
        stub_renderer.convert_fallback = AsyncMock(return_value=None)
        return ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
            pdf_generator=None,
            pdf_semaphore=None,
            renderer=stub_renderer,
        )

    @pytest.mark.asyncio
    async def test_process_content_text_format(self, executor):
        """Test text format conversion through shared convert()."""
        content = b"<html><body><p>Hello World</p></body></html>"
        metadata = {"content_type": "text/html"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="text",
        )

        assert "Hello World" in result

    @pytest.mark.asyncio
    async def test_process_content_markdown_format(self, executor):
        """Test markdown format conversion through shared convert()."""
        content = b"<html><body><h1>Title</h1></body></html>"
        metadata = {"content_type": "text/html"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="markdown",
        )

        assert "# Title" in result

    @pytest.mark.asyncio
    async def test_process_content_html_format(self, executor):
        """Test HTML format returns decoded content."""
        content = b"<html><body>Hello</body></html>"
        metadata = {"content_type": "text/html"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="html",
        )

        assert result == "<html><body>Hello</body></html>"

    @pytest.mark.asyncio
    async def test_process_content_json_format(self, executor):
        """Test JSON format returns serialized JSON envelope with base64 content."""
        content = b'{"key": "value"}'
        metadata = {"content_type": "application/json"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="json",
        )

        assert isinstance(result, str)
        envelope = json.loads(result)
        assert envelope["success"] is True
        assert envelope["url"] == "https://example.com"
        assert envelope["size"] == len(content)
        assert envelope["content_type"] == "application/json"
        assert base64.b64decode(envelope["content"]) == content

    @pytest.mark.asyncio
    async def test_process_content_raw_format(self, executor):
        """Test raw format returns raw bytes."""
        content = b"\x00\x01\x02\x03"
        metadata = {"content_type": "application/octet-stream"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="raw",
        )

        assert result == content

    @pytest.mark.asyncio
    async def test_process_content_pdf_no_semaphore(self, mock_http_client, mock_storage):
        """Test PDF format raises when semaphore not configured."""
        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
            pdf_generator=None,
            pdf_semaphore=None,
        )
        content = b"<html>test</html>"
        metadata = {"content_type": "text/html"}

        with pytest.raises(RuntimeError, match="semaphore not configured"):
            await executor._process_content(
                url="https://example.com",
                content=content,
                metadata=metadata,
                format="pdf",
            )

    @pytest.mark.asyncio
    async def test_process_content_pdf_with_semaphore(self, mock_http_client, mock_storage):
        """Test PDF format with semaphore configured."""
        import asyncio

        semaphore = asyncio.Semaphore(1)
        executor = ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
            pdf_generator=None,
            pdf_semaphore=semaphore,
        )

        content = b"<html>test</html>"
        metadata = {"content_type": "text/html"}

        with patch(
            "downloader.pdf_generator.generate_pdf_from_url",
            new=AsyncMock(return_value=b"%PDF-1.4 content"),
        ) as mock_pdf:
            result = await executor._process_content(
                url="https://example.com",
                content=content,
                metadata=metadata,
                format="pdf",
            )

            mock_pdf.assert_awaited_once()
            assert mock_pdf.await_args is not None
            assert mock_pdf.await_args.args == ("https://example.com",)
            assert mock_pdf.await_args.kwargs["max_output_bytes"] == 52428800
            assert mock_pdf.await_args.kwargs["egress_policy"] is not None
            assert result == b"%PDF-1.4 content"

    @pytest.mark.asyncio
    async def test_process_content_unknown_format(self, executor):
        """Test unknown format returns raw content with warning."""
        content = b"some content"
        metadata = {"content_type": "text/plain"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="unknown_format",
        )

        assert result == content


@pytest.mark.unit
@pytest.mark.asyncio
async def test_list_schedules_round_trips_cron_expression():
    """Test POST -> GET /schedules and GET /schedules/{id} preserve submitted cron_expression."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from downloader.config import Settings
    from downloader.routes import schedules as schedules_routes

    svc = SchedulerService(redis_uri=None, settings=SchedulerConfig(job_store_type="memory"))
    await svc.start(paused=True)

    app = FastAPI()
    app.include_router(schedules_routes.router)
    app.state.scheduler = svc
    app.state.execution_storage = AsyncMock(spec=ExecutionStorage)
    app.state.settings = Settings()

    try:
        with (
            patch(
                "downloader.routes.schedules.validate_url_async",
                new=AsyncMock(side_effect=lambda u, _s: u),
            ),
            TestClient(app) as client,
        ):
            submitted = "30 9 1 6 *"
            create_resp = client.post(
                "/schedules",
                json={
                    "name": "Annual June 1st",
                    "url": "https://example.com/report",
                    "cron_expression": submitted,
                    "format": "text",
                },
            )
            assert create_resp.status_code == 200
            created = create_resp.json()
            assert created["cron_expression"] == submitted

            list_resp = client.get("/schedules")
            assert list_resp.status_code == 200
            listed = list_resp.json()
            assert listed["total"] == 1
            assert listed["schedules"][0]["cron_expression"] == submitted

            get_resp = client.get(f"/schedules/{created['id']}")
            assert get_resp.status_code == 200
            assert get_resp.json()["cron_expression"] == submitted

            del_resp = client.delete(f"/schedules/{created['id']}")
            assert del_resp.status_code == 200
            app.state.execution_storage.delete_executions.assert_awaited_once_with(created["id"])
    finally:
        await svc.shutdown(wait=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_scheduled_download_job_skips_when_run_lock_held():
    """Test two active schedulers do not duplicate a run when Redis SET NX EX lock is held."""
    from downloader.routes import schedules as schedules_routes

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=[True, None])
    mock_redis.eval = AsyncMock(return_value=1)
    mock_storage = AsyncMock(spec=ExecutionStorage)
    mock_storage.redis = mock_redis

    mock_executor = AsyncMock()
    mock_executor.storage = mock_storage
    mock_executor.execute = AsyncMock(
        return_value=ScheduleExecution(
            execution_id="exec-1",
            schedule_id="sched-lock",
            status=ExecutionStatus.COMPLETED,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
            duration=0.1,
            success=True,
            content_size=10,
            error_message=None,
            attempt=1,
        )
    )

    prev_executor = schedules_routes._executor
    schedules_routes.set_executor(mock_executor)
    try:
        await schedules_routes.scheduled_download_job(
            "sched-lock", "https://example.com", "text", None
        )
        await schedules_routes.scheduled_download_job(
            "sched-lock", "https://example.com", "text", None
        )
        assert mock_executor.execute.await_count == 1
        assert mock_redis.set.await_count == 2
        # Token-owned release Lua script runs once after execution finishes (`R110`).
        assert mock_redis.eval.await_count == 1
    finally:
        schedules_routes.set_executor(prev_executor)


@pytest.mark.unit
def test_reconstruct_legacy_cron_expr_handles_cron_and_or_triggers() -> None:
    """Legacy persisted triggers without `cron_expression` kwarg reconstruct cleanly (`R114`)."""
    from downloader.routes.schedules import _reconstruct_legacy_cron_expr

    probe = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)

    t_simple = cron_trigger_from_unix_crontab("30 9 * * 1-5")
    reconstructed_simple = _reconstruct_legacy_cron_expr(t_simple)
    assert reconstructed_simple == "30 9 * * mon,tue,wed,thu,fri"
    assert cron_trigger_from_unix_crontab(reconstructed_simple).get_next_fire_time(
        None, probe
    ) == t_simple.get_next_fire_time(None, probe)

    t_or = cron_trigger_from_unix_crontab("0 9 13 * 5")
    reconstructed_or = _reconstruct_legacy_cron_expr(t_or)
    assert reconstructed_or == "0 9 13 * fri"
    assert cron_trigger_from_unix_crontab(reconstructed_or).get_next_fire_time(
        None, probe
    ) == t_or.get_next_fire_time(None, probe)

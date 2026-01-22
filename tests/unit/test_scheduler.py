"""Unit tests for scheduler components.

Tests for:
- ScheduleCreate and ScheduleExecution model validation
- Cron expression validation
- ExecutionStorage Redis operations
- SchedulerService initialization and lifecycle
- ScheduledJobExecutor content processing
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.downloader.config import SchedulerConfig
from src.downloader.models.schedule import (
    ExecutionStatus,
    ScheduleCreate,
    ScheduleExecution,
    ScheduleResponse,
)
from src.downloader.scheduler.executor import (
    MAX_ATTEMPTS,
    RETRY_DELAYS,
    ScheduledJobExecutor,
)
from src.downloader.scheduler.service import SchedulerService
from src.downloader.scheduler.storage import DEFAULT_EXECUTION_TTL, ExecutionStorage

# ============= Model Validation Tests =============


@pytest.mark.unit
class TestScheduleModelValidation:
    """Tests for ScheduleCreate and related model validation."""

    def test_schedule_create_valid(self):
        """Test creating a valid ScheduleCreate model."""
        schedule = ScheduleCreate(
            name="Daily Download",
            url="https://example.com/page",
            cron_expression="0 9 * * *",
            format="text",
        )
        assert schedule.name == "Daily Download"
        assert schedule.url == "https://example.com/page"
        assert schedule.cron_expression == "0 9 * * *"
        assert schedule.format == "text"
        assert schedule.enabled is True
        assert schedule.headers is None

    def test_schedule_create_name_min_length(self):
        """Test that name must have minimum length of 1."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="",
                url="https://example.com",
                cron_expression="0 9 * * *",
            )
        assert "String should have at least 1 character" in str(exc_info.value)

    def test_schedule_create_name_max_length(self):
        """Test that name must not exceed 100 characters."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="x" * 101,
                url="https://example.com",
                cron_expression="0 9 * * *",
            )
        assert "String should have at most 100 characters" in str(exc_info.value)

    def test_schedule_create_url_required(self):
        """Test that url is a required field."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                cron_expression="0 9 * * *",
            )
        assert "url" in str(exc_info.value)

    def test_schedule_create_url_max_length(self):
        """Test that url must not exceed 2048 characters."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com/" + "x" * 2040,
                cron_expression="0 9 * * *",
            )
        assert "String should have at most 2048 characters" in str(exc_info.value)

    def test_schedule_create_default_format(self):
        """Test that format defaults to 'text'."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
        )
        assert schedule.format == "text"

    def test_schedule_create_valid_formats(self):
        """Test that all valid formats are accepted."""
        valid_formats = ["text", "html", "markdown", "pdf", "json", "raw"]
        for fmt in valid_formats:
            schedule = ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="0 9 * * *",
                format=fmt,
            )
            assert schedule.format == fmt

    def test_schedule_create_invalid_format(self):
        """Test that invalid formats are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="0 9 * * *",
                format="invalid_format",
            )
        assert "Input should be" in str(exc_info.value)

    def test_schedule_create_headers_optional(self):
        """Test that headers can be None or a dict."""
        # None
        schedule1 = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
            headers=None,
        )
        assert schedule1.headers is None

        # Dict
        schedule2 = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
            headers={"Authorization": "Bearer token"},
        )
        assert schedule2.headers == {"Authorization": "Bearer token"}

    def test_schedule_create_enabled_default(self):
        """Test that enabled defaults to True."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
        )
        assert schedule.enabled is True

    def test_schedule_create_enabled_false(self):
        """Test setting enabled to False."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
            enabled=False,
        )
        assert schedule.enabled is False

    def test_schedule_response_model(self):
        """Test ScheduleResponse model fields."""
        response = ScheduleResponse(
            id="uuid-123",
            name="Test Schedule",
            url="https://example.com",
            cron_expression="0 9 * * *",
            format="text",
            headers=None,
            enabled=True,
            created_at=datetime.now(timezone.utc),
            next_run_time=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        assert response.id == "uuid-123"
        assert response.name == "Test Schedule"
        assert response.enabled is True

    def test_schedule_execution_model(self):
        """Test ScheduleExecution model fields."""
        execution = ScheduleExecution(
            execution_id="exec-123",
            schedule_id="sched-456",
            status=ExecutionStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration=1.5,
            success=True,
            content_size=1024,
            error_message=None,
            attempt=1,
        )
        assert execution.execution_id == "exec-123"
        assert execution.status == ExecutionStatus.COMPLETED
        assert execution.success is True
        assert execution.attempt == 1

    def test_schedule_execution_attempt_min_value(self):
        """Test that attempt must be >= 1."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleExecution(
                execution_id="exec-123",
                schedule_id="sched-456",
                status=ExecutionStatus.COMPLETED,
                started_at=datetime.now(timezone.utc),
                success=True,
                attempt=0,
            )
        assert "greater than or equal to 1" in str(exc_info.value)

    def test_execution_status_enum(self):
        """Test ExecutionStatus enum values."""
        assert ExecutionStatus.PENDING.value == "pending"
        assert ExecutionStatus.RUNNING.value == "running"
        assert ExecutionStatus.COMPLETED.value == "completed"
        assert ExecutionStatus.FAILED.value == "failed"


# ============= Cron Expression Validation Tests =============


@pytest.mark.unit
class TestCronExpressionValidation:
    """Tests for cron expression validation via APScheduler CronTrigger."""

    def test_valid_cron_every_minute(self):
        """Test '* * * * *' is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="* * * * *",
        )
        assert schedule.cron_expression == "* * * * *"

    def test_valid_cron_daily_9am(self):
        """Test '0 9 * * *' (daily at 9 AM) is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * *",
        )
        assert schedule.cron_expression == "0 9 * * *"

    def test_valid_cron_weekdays(self):
        """Test '0 9 * * 1-5' (weekdays at 9 AM) is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 9 * * 1-5",
        )
        assert schedule.cron_expression == "0 9 * * 1-5"

    def test_valid_cron_every_15_minutes(self):
        """Test '*/15 * * * *' (every 15 minutes) is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="*/15 * * * *",
        )
        assert schedule.cron_expression == "*/15 * * * *"

    def test_valid_cron_monthly(self):
        """Test '0 0 1 * *' (first day of month at midnight) is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="0 0 1 * *",
        )
        assert schedule.cron_expression == "0 0 1 * *"

    def test_valid_cron_complex(self):
        """Test '30 4 1,15 * 5' (complex expression) is accepted."""
        schedule = ScheduleCreate(
            name="Test",
            url="https://example.com",
            cron_expression="30 4 1,15 * 5",
        )
        assert schedule.cron_expression == "30 4 1,15 * 5"

    def test_invalid_cron_wrong_field_count(self):
        """Test that expressions with wrong field count are rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="* * *",  # Only 3 fields
            )
        # Either fails cron validation or min_length
        error_str = str(exc_info.value)
        assert "Invalid cron expression" in error_str or "at least 9 characters" in error_str

    def test_invalid_cron_bad_minute(self):
        """Test that minute > 59 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="60 * * * *",
            )
        assert "Invalid cron expression" in str(exc_info.value)

    def test_invalid_cron_bad_hour(self):
        """Test that hour > 23 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="0 24 * * *",
            )
        assert "Invalid cron expression" in str(exc_info.value)

    def test_invalid_cron_bad_day(self):
        """Test that day > 31 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="0 0 32 * *",
            )
        assert "Invalid cron expression" in str(exc_info.value)

    def test_invalid_cron_bad_month(self):
        """Test that month > 12 is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="0 0 * 13 *",
            )
        assert "Invalid cron expression" in str(exc_info.value)

    def test_invalid_cron_empty(self):
        """Test that empty string is rejected."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="",
            )
        assert "at least 9 characters" in str(exc_info.value)

    def test_cron_expression_min_length(self):
        """Test cron_expression min_length=9 validation."""
        with pytest.raises(ValidationError) as exc_info:
            ScheduleCreate(
                name="Test",
                url="https://example.com",
                cron_expression="* * * *",  # Only 7 chars
            )
        assert "at least 9 characters" in str(exc_info.value)


# ============= ExecutionStorage Tests =============


@pytest.mark.unit
class TestExecutionStorageOperations:
    """Tests for ExecutionStorage Redis operations."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client."""
        return AsyncMock()

    @pytest.fixture
    def storage(self, mock_redis):
        """Create ExecutionStorage with mocked Redis."""
        return ExecutionStorage(mock_redis, ttl=DEFAULT_EXECUTION_TTL)

    @pytest.fixture
    def sample_execution(self):
        """Create a sample execution for testing."""
        return ScheduleExecution(
            execution_id="exec-123",
            schedule_id="sched-456",
            status=ExecutionStatus.COMPLETED,
            started_at=datetime.now(timezone.utc),
            completed_at=datetime.now(timezone.utc),
            duration=1.5,
            success=True,
            content_size=1024,
            error_message=None,
            attempt=1,
        )

    @pytest.mark.asyncio
    async def test_store_execution_success(self, storage, mock_redis, sample_execution):
        """Test storing execution record calls setex and zadd."""
        await storage.store_execution(sample_execution)

        mock_redis.setex.assert_called_once()
        mock_redis.zadd.assert_called_once()
        mock_redis.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_execution_sets_ttl(self, storage, mock_redis, sample_execution):
        """Test that TTL is applied to execution record."""
        await storage.store_execution(sample_execution)

        # Verify setex was called with correct TTL
        call_args = mock_redis.setex.call_args
        assert call_args[0][1] == DEFAULT_EXECUTION_TTL

    @pytest.mark.asyncio
    async def test_store_execution_key_format(self, storage, mock_redis, sample_execution):
        """Test that correct key format is used."""
        await storage.store_execution(sample_execution)

        expected_key = (
            f"schedule:execution:{sample_execution.schedule_id}:{sample_execution.execution_id}"
        )
        call_args = mock_redis.setex.call_args
        assert call_args[0][0] == expected_key

    @pytest.mark.asyncio
    async def test_store_execution_adds_to_sorted_set(self, storage, mock_redis, sample_execution):
        """Test that execution is added to sorted set by timestamp."""
        await storage.store_execution(sample_execution)

        list_key = f"schedule:executions:{sample_execution.schedule_id}"
        call_args = mock_redis.zadd.call_args
        assert call_args[0][0] == list_key

    @pytest.mark.asyncio
    async def test_get_execution_found(self, storage, mock_redis, sample_execution):
        """Test retrieving an existing execution record."""
        mock_redis.get.return_value = sample_execution.model_dump_json()

        result = await storage.get_execution("sched-456", "exec-123")

        assert result is not None
        assert result.execution_id == "exec-123"
        assert result.schedule_id == "sched-456"

    @pytest.mark.asyncio
    async def test_get_execution_not_found(self, storage, mock_redis):
        """Test retrieving a non-existent execution returns None."""
        mock_redis.get.return_value = None

        result = await storage.get_execution("sched-456", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_executions_with_pagination(self, storage, mock_redis, sample_execution):
        """Test get_executions with pagination parameters."""
        mock_redis.zrevrange.return_value = [b"exec-1", b"exec-2"]
        mock_redis.get.return_value = sample_execution.model_dump_json()

        await storage.get_executions("sched-456", limit=10, offset=5)

        # zrevrange should be called with start=5, end=14 (offset + limit - 1)
        mock_redis.zrevrange.assert_called_once()
        call_args = mock_redis.zrevrange.call_args[0]
        assert call_args[1] == 5  # start
        assert call_args[2] == 14  # end

    @pytest.mark.asyncio
    async def test_get_executions_empty_list(self, storage, mock_redis):
        """Test get_executions returns empty list when no executions."""
        mock_redis.zrevrange.return_value = []

        result = await storage.get_executions("sched-456")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_executions_handles_bytes(self, storage, mock_redis, sample_execution):
        """Test that bytes from Redis are decoded properly."""
        mock_redis.zrevrange.return_value = [b"exec-123"]
        mock_redis.get.return_value = sample_execution.model_dump_json().encode("utf-8")

        result = await storage.get_executions("sched-456")

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_get_execution_count(self, storage, mock_redis):
        """Test get_execution_count calls zcard."""
        mock_redis.zcard.return_value = 42

        result = await storage.get_execution_count("sched-456")

        assert result == 42
        mock_redis.zcard.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_execution_count_zero(self, storage, mock_redis):
        """Test get_execution_count returns 0 when None."""
        mock_redis.zcard.return_value = None

        result = await storage.get_execution_count("sched-456")

        assert result == 0

    @pytest.mark.asyncio
    async def test_delete_executions_deletes_all_keys(self, storage, mock_redis):
        """Test delete_executions deletes all execution keys."""
        mock_redis.zrange.return_value = [b"exec-1", b"exec-2"]
        mock_redis.delete.return_value = 3

        result = await storage.delete_executions("sched-456")

        assert result == 3
        mock_redis.delete.assert_called_once()
        # Should delete list key + 2 execution keys
        assert len(mock_redis.delete.call_args[0]) == 3

    @pytest.mark.asyncio
    async def test_delete_executions_empty_schedule(self, storage, mock_redis):
        """Test delete_executions returns 0 when no executions exist."""
        mock_redis.zrange.return_value = []

        result = await storage.delete_executions("sched-456")

        assert result == 0
        mock_redis.delete.assert_not_called()

    def test_key_format_execution(self, storage):
        """Test _get_execution_key returns correct format."""
        key = storage._get_execution_key("sched-123", "exec-456")
        assert key == "schedule:execution:sched-123:exec-456"

    def test_key_format_executions_list(self, storage):
        """Test _get_executions_list_key returns correct format."""
        key = storage._get_executions_list_key("sched-123")
        assert key == "schedule:executions:sched-123"


# ============= SchedulerService Tests =============


@pytest.mark.unit
class TestSchedulerServiceInit:
    """Tests for SchedulerService initialization."""

    def test_init_with_memory_job_store_by_config(self):
        """Test in-memory job store when configured."""
        settings = SchedulerConfig(job_store_type="memory")
        service = SchedulerService(redis_uri="redis://localhost:6379", settings=settings)

        assert service._scheduler is not None
        assert service._started is False

    def test_init_with_memory_job_store_when_no_redis(self):
        """Test in-memory job store when redis_uri is None."""
        settings = SchedulerConfig(job_store_type="redis")
        service = SchedulerService(redis_uri=None, settings=settings)

        assert service._scheduler is not None

    def test_init_redis_fallback_on_error(self):
        """Test fallback to memory store when Redis connection fails."""
        settings = SchedulerConfig(job_store_type="redis")

        with patch(
            "src.downloader.scheduler.service._get_redis_job_store",
            side_effect=Exception("Connection refused"),
        ):
            service = SchedulerService(redis_uri="redis://badhost:6379", settings=settings)

            # Should still initialize with memory store
            assert service._scheduler is not None

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

        # Should not raise
        service.remove_job("job-123")

    def test_set_executor(self, service):
        """Test set_executor stores the executor."""
        mock_executor = MagicMock()

        service.set_executor(mock_executor)

        assert service.executor is mock_executor

    def test_executor_property_none(self, service):
        """Test executor property returns None when not set."""
        assert service.executor is None


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
        """Create a ScheduledJobExecutor with mocks."""
        return ScheduledJobExecutor(
            http_client=mock_http_client,
            storage=mock_storage,
            pdf_generator=None,
            pdf_semaphore=None,
        )

    @pytest.mark.asyncio
    async def test_process_content_text_format(self, executor):
        """Test text format conversion."""
        content = b"<html><body><p>Hello World</p></body></html>"
        metadata = {"content_type": "text/html"}

        with patch(
            "src.downloader.scheduler.executor.convert_content_to_text",
            return_value="Hello World",
        ) as mock_convert:
            result = await executor._process_content(
                url="https://example.com",
                content=content,
                metadata=metadata,
                format="text",
            )

            mock_convert.assert_called_once_with(content, "text/html")
            assert result == "Hello World"

    @pytest.mark.asyncio
    async def test_process_content_markdown_format(self, executor):
        """Test markdown format conversion."""
        content = b"<html><body><h1>Title</h1></body></html>"
        metadata = {"content_type": "text/html"}

        with patch(
            "src.downloader.scheduler.executor.convert_content_to_markdown",
            return_value="# Title",
        ) as mock_convert:
            result = await executor._process_content(
                url="https://example.com",
                content=content,
                metadata=metadata,
                format="markdown",
            )

            mock_convert.assert_called_once_with(content, "text/html")
            assert result == "# Title"

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
        """Test JSON format returns raw bytes."""
        content = b'{"key": "value"}'
        metadata = {"content_type": "application/json"}

        result = await executor._process_content(
            url="https://example.com",
            content=content,
            metadata=metadata,
            format="json",
        )

        assert result == content

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
    async def test_process_content_pdf_no_semaphore(self, executor):
        """Test PDF format raises when semaphore not configured."""
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
            "src.downloader.scheduler.executor.generate_pdf_from_url",
            return_value=b"%PDF-1.4 content",
        ) as mock_pdf:
            result = await executor._process_content(
                url="https://example.com",
                content=content,
                metadata=metadata,
                format="pdf",
            )

            mock_pdf.assert_called_once_with("https://example.com")
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

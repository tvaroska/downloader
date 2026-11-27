"""Unit tests for schedule models, UNIX cron validation, and ExecutionStorage."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from downloader.models.schedule import (
    ExecutionStatus,
    ScheduleCreate,
    ScheduleExecution,
    ScheduleResponse,
    cron_trigger_from_unix_crontab,
)
from downloader.scheduler.storage import DEFAULT_EXECUTION_TTL, ExecutionStorage


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
            ScheduleCreate.model_validate(
                {
                    "name": "Test",
                    "cron_expression": "0 9 * * *",
                }
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
            created_at=datetime.now(UTC),
            next_run_time=datetime.now(UTC) + timedelta(hours=1),
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
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
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
                started_at=datetime.now(UTC),
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

    def test_weekday_range_fires_monday_to_friday(self):
        """Test UNIX 0=Sunday semantics: 0 9 * * 1-5 fires Mon-Fri, not Tue-Sat."""
        trigger = cron_trigger_from_unix_crontab("0 9 * * 1-5")
        # Sunday 2026-11-15 00:00 UTC
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        fired_weekdays: list[int] = []
        for _ in range(5):
            next_fire = trigger.get_next_fire_time(None, cursor)
            assert next_fire is not None
            fired_weekdays.append(next_fire.weekday())  # Python: Mon=0 .. Sun=6
            cursor = next_fire + timedelta(minutes=1)
        assert fired_weekdays == [0, 1, 2, 3, 4]

        # Weekend list 0,6 -> Sun (6) and Sat (5)
        weekend = cron_trigger_from_unix_crontab("0 9 * * 0,6")
        sat_fire = weekend.get_next_fire_time(None, datetime(2026, 11, 16, 0, 0, tzinfo=UTC))
        assert sat_fire is not None and sat_fire.weekday() == 5
        sun_fire = weekend.get_next_fire_time(None, sat_fire + timedelta(minutes=1))
        assert sun_fire is not None and sun_fire.weekday() == 6

        # Stepped range 1-5/2 -> Mon (0), Wed (2), Fri (4)
        stepped = cron_trigger_from_unix_crontab("0 9 * * 1-5/2")
        stepped_days: list[int] = []
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        for _ in range(3):
            next_fire = stepped.get_next_fire_time(None, cursor)
            assert next_fire is not None
            stepped_days.append(next_fire.weekday())
            cursor = next_fire + timedelta(minutes=1)
        assert stepped_days == [0, 2, 4]

        # Sunday alias 7 -> Sun (6)
        sun_seven = cron_trigger_from_unix_crontab("0 9 * * 7")
        fire_seven = sun_seven.get_next_fire_time(None, datetime(2026, 11, 16, 0, 0, tzinfo=UTC))
        assert fire_seven is not None and fire_seven.weekday() == 6

    def test_weekday_step_and_0_7_range_use_unix_numbering(self):
        """`*/2` fires Sun/Tue/Thu/Sat, `0-7` fires all 7 days, DOM+DOW uses OR, and tz is UTC (`R79`, `R112`)."""
        # 0 9 * * */2 -> Sun (6), Tue (1), Thu (3), Sat (5)
        step_trigger = cron_trigger_from_unix_crontab("0 9 * * */2")
        assert getattr(step_trigger, "timezone", None) == UTC
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)  # Sunday
        step_days: list[int] = []
        for _ in range(4):
            nxt = step_trigger.get_next_fire_time(None, cursor)
            assert nxt is not None
            step_days.append(nxt.weekday())
            cursor = nxt + timedelta(minutes=1)
        assert step_days == [6, 1, 3, 5]

        # 0 9 * * 0-7 -> every day of the week (7 consecutive days)
        all_days_trigger = cron_trigger_from_unix_crontab("0 9 * * 0-7")
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        all_days: list[int] = []
        for _ in range(7):
            nxt = all_days_trigger.get_next_fire_time(None, cursor)
            assert nxt is not None
            all_days.append(nxt.weekday())
            cursor = nxt + timedelta(minutes=1)
        assert all_days == [6, 0, 1, 2, 3, 4, 5]

        # Restricted DOM + DOW uses UNIX OR semantics: 0 9 13 * 5 fires on 13th AND every Friday
        or_trigger = cron_trigger_from_unix_crontab("0 9 13 * 5")
        # 2026-11-12 is Thursday -> next fire is Fri Nov 13, then Fri Nov 20, ..., and Sun Dec 13
        fire1 = or_trigger.get_next_fire_time(None, datetime(2026, 12, 1, 0, 0, tzinfo=UTC))
        assert fire1 is not None and (fire1.day, fire1.weekday()) == (4, 4)  # Fri Dec 4
        fire_dec13 = or_trigger.get_next_fire_time(None, datetime(2026, 12, 12, 0, 0, tzinfo=UTC))
        assert fire_dec13 is not None and (fire_dec13.day, fire_dec13.weekday()) == (
            13,
            6,
        )  # Sun Dec 13

        # Unmappable DOW token is rejected
        with pytest.raises(ValueError):
            cron_trigger_from_unix_crontab("0 9 * * 8")

    def test_sunday_ranges_and_steps(self):
        """Ranges starting at 0 or ending at 7 include Sunday (`R113`)."""
        # 0-3 -> Sun (6), Mon (0), Tue (1), Wed (2)
        t_0_3 = cron_trigger_from_unix_crontab("0 9 * * 0-3")
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        days_0_3 = []
        for _ in range(4):
            nxt = t_0_3.get_next_fire_time(None, cursor)
            assert nxt is not None
            days_0_3.append(nxt.weekday())
            cursor = nxt + timedelta(minutes=1)
        assert days_0_3 == [6, 0, 1, 2]

        # 5-7 -> Fri (4), Sat (5), Sun (6)
        t_5_7 = cron_trigger_from_unix_crontab("0 9 * * 5-7")
        cursor = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)  # Friday
        days_5_7 = []
        for _ in range(3):
            nxt = t_5_7.get_next_fire_time(None, cursor)
            assert nxt is not None
            days_5_7.append(nxt.weekday())
            cursor = nxt + timedelta(minutes=1)
        assert days_5_7 == [4, 5, 6]

        # 0-6/2 -> Sun (6), Tue (1), Thu (3), Sat (5)
        t_0_6_step = cron_trigger_from_unix_crontab("0 9 * * 0-6/2")
        cursor = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
        days_step = []
        for _ in range(4):
            nxt = t_0_6_step.get_next_fire_time(None, cursor)
            assert nxt is not None
            days_step.append(nxt.weekday())
            cursor = nxt + timedelta(minutes=1)
        assert days_step == [6, 1, 3, 5]


@pytest.mark.unit
def test_schedule_create_rejects_unknown_fields() -> None:
    """`ScheduleCreate` uses `extra='forbid'` so typos like `enable=False` fail validation (`R105`)."""
    with pytest.raises(ValidationError):
        ScheduleCreate.model_validate(
            {
                "name": "Daily",
                "url": "https://example.com/page",
                "cron_expression": "0 9 * * *",
                "enable": False,
            }
        )


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
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
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

    @pytest.mark.asyncio
    async def test_total_excludes_expired_executions(self):
        """Test get_execution_count and get_executions trim expired IDs from sorted set."""

        class _InMemoryZSetRedis:
            def __init__(self) -> None:
                self.kv: dict[str, tuple[str, float]] = {}
                self.zsets: dict[str, dict[str, float]] = {}

            async def setex(self, key: str, ttl: int, value: str) -> bool:
                self.kv[key] = (value, ttl)
                return True

            async def get(self, key: str) -> str | None:
                entry = self.kv.get(key)
                return entry[0] if entry else None

            async def zadd(self, key: str, mapping: dict[str, float]) -> int:
                zset = self.zsets.setdefault(key, {})
                zset.update(mapping)
                return len(mapping)

            async def zremrangebyscore(self, key: str, min_score: str, max_score: float) -> int:
                zset = self.zsets.get(key, {})
                to_remove = [
                    k for k, score in zset.items() if min_score == "-inf" and score <= max_score
                ]
                for k in to_remove:
                    del zset[k]
                return len(to_remove)

            async def zrevrange(self, key: str, start: int, end: int) -> list[bytes]:
                zset = self.zsets.get(key, {})
                ordered = sorted(zset.items(), key=lambda item: item[1], reverse=True)
                sliced = ordered[start : end + 1]
                return [k.encode("utf-8") for k, _ in sliced]

            async def zcard(self, key: str) -> int:
                return len(self.zsets.get(key, {}))

            async def expire(self, key: str, ttl: int) -> bool:
                return key in self.zsets

        fake_redis = _InMemoryZSetRedis()
        storage = ExecutionStorage(fake_redis, ttl=3600)
        now = datetime.now(UTC)

        expired_exec = ScheduleExecution(
            execution_id="exec-expired",
            schedule_id="sched-1",
            status=ExecutionStatus.COMPLETED,
            started_at=now - timedelta(seconds=7200),
            completed_at=now - timedelta(seconds=7199),
            duration=1.0,
            success=True,
            content_size=100,
            error_message=None,
            attempt=1,
        )
        fresh_exec = ScheduleExecution(
            execution_id="exec-fresh",
            schedule_id="sched-1",
            status=ExecutionStatus.COMPLETED,
            started_at=now,
            completed_at=now,
            duration=1.0,
            success=True,
            content_size=200,
            error_message=None,
            attempt=1,
        )

        await storage.store_execution(expired_exec)
        await storage.store_execution(fresh_exec)
        # Simulate Redis TTL expiring the old execution record key
        fake_redis.kv.pop(storage._get_execution_key("sched-1", "exec-expired"), None)

        assert await storage.get_execution_count("sched-1") == 1
        executions = await storage.get_executions("sched-1")
        assert [e.execution_id for e in executions] == ["exec-fresh"]

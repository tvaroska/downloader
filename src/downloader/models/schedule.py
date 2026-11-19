"""Pydantic models for schedule CRUD operations."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from apscheduler.triggers.base import BaseTrigger

_UNIX_DOW_NAMES: tuple[str, ...] = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NAME_TO_DOW_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(_UNIX_DOW_NAMES)}


def _parse_dow_int(token: str) -> int:
    """Parse a single UNIX day-of-week token (0-7 or sun..sat) to an integer 0..7."""
    cleaned = token.strip().lower()
    if cleaned in _NAME_TO_DOW_INDEX:
        return _NAME_TO_DOW_INDEX[cleaned]
    if cleaned.isdigit():
        value = int(cleaned)
        if 0 <= value <= 7:
            return value
    raise ValueError(f"Invalid day-of-week value: {token!r}")


def _expand_dow_range(base: str, step: int, raw_item: str) -> list[int]:
    bounds = base.split("-")
    if len(bounds) != 2:
        raise ValueError(f"Invalid day-of-week range: {raw_item!r}")
    start_raw, end_raw = bounds[0].strip(), bounds[1].strip()
    start_idx = _parse_dow_int(start_raw)
    end_idx = _parse_dow_int(end_raw)
    if start_idx == 7 and end_idx < 7:
        start_idx = 0
    if end_idx == 0 and start_idx > 0 and end_raw.lower() == "sun":
        end_idx = 7
    if start_idx > end_idx:
        raise ValueError(f"Invalid day-of-week range: {raw_item!r}")
    return [day % 7 for day in range(start_idx, end_idx + 1, step)]


def _expand_unix_dow_item(item: str) -> list[int]:
    """Expand one comma-separated UNIX day-of-week token into canonical 0..6 indices (0=Sun)."""
    token = item.strip()
    if not token:
        raise ValueError("Empty day-of-week token")
    parts = token.split("/")
    if len(parts) > 2:
        raise ValueError(f"Invalid day-of-week step expression: {item!r}")
    base = parts[0].strip()
    has_step = len(parts) == 2
    step = 1
    if has_step:
        step_str = parts[1].strip()
        if not step_str.isdigit() or int(step_str) <= 0:
            raise ValueError(f"Invalid day-of-week step value: {item!r}")
        step = int(step_str)

    if base == "*":
        return list(range(0, 7, step))
    if "-" in base:
        return _expand_dow_range(base, step, item)
    single = _parse_dow_int(base) % 7
    return list(range(single, 7, step)) if has_step else [single]


def _translate_unix_dow_field(field: str) -> str:
    """Expand a full UNIX day-of-week cron field to explicit APScheduler day names."""
    cleaned = field.strip()
    if cleaned == "*":
        return "*"
    indices: list[int] = []
    for item in cleaned.split(","):
        indices.extend(_expand_unix_dow_item(item))
    unique_indices = list(dict.fromkeys(indices))
    if not unique_indices:
        raise ValueError(f"Invalid day-of-week field: {field!r}")
    return ",".join(_UNIX_DOW_NAMES[idx] for idx in unique_indices)


def cron_trigger_from_unix_crontab(expr: str) -> BaseTrigger:
    """Build a UTC APScheduler trigger from a 5-field UNIX crontab (0/7=Sun..6=Sat, DOM|DOW OR)."""
    from apscheduler.triggers.combining import OrTrigger
    from apscheduler.triggers.cron import CronTrigger

    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Wrong number of fields; got {len(parts)}, expected 5")

    minute, hour, dom, month, dow = parts
    translated_dow = _translate_unix_dow_field(dow)
    if dom != "*" and dow != "*":
        dom_trigger = CronTrigger.from_crontab(f"{minute} {hour} {dom} {month} *", timezone=UTC)
        dow_trigger = CronTrigger.from_crontab(
            f"{minute} {hour} * {month} {translated_dow}",
            timezone=UTC,
        )
        return OrTrigger([dom_trigger, dow_trigger])

    return CronTrigger.from_crontab(
        f"{minute} {hour} {dom} {month} {translated_dow}",
        timezone=UTC,
    )


class ExecutionStatus(str, Enum):
    """Status of a scheduled job execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleCreate(BaseModel):
    """Request model for creating a scheduled download job."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="Human-readable name for the schedule",
    )
    url: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description="URL to download on schedule",
    )
    cron_expression: str = Field(
        ...,
        min_length=9,
        max_length=100,
        description="Cron expression (5 fields: minute hour day month day_of_week)",
    )
    format: Literal["text", "html", "markdown", "pdf", "screenshot", "json", "raw"] = Field(
        default="text",
        description="Output format for downloaded content",
    )
    headers: dict[str, str] | None = Field(
        default=None,
        description="Optional custom HTTP headers for the request",
    )
    enabled: bool = Field(
        default=True,
        description="Whether the schedule is active",
    )
    webhook_url: str | None = Field(
        default=None,
        max_length=2048,
        description="Optional HTTP(S) callback URL notified after each scheduled execution",
    )
    webhook_secret: str | None = Field(
        default=None,
        max_length=256,
        description="Optional HMAC-SHA256 signing secret (never returned in API responses)",
    )

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        """Validate cron expression using UNIX 0=Sunday semantics via APScheduler."""
        try:
            cron_trigger_from_unix_crontab(v)
        except ValueError as e:
            raise ValueError(f"Invalid cron expression: {e}") from e
        return v


class ScheduleResponse(BaseModel):
    """Response model for a scheduled job."""

    id: str = Field(..., description="Unique schedule identifier (UUID)")
    name: str = Field(..., description="Schedule name")
    url: str = Field(..., description="URL to download")
    cron_expression: str = Field(..., description="Cron expression")
    format: str = Field(..., description="Output format")
    headers: dict[str, str] | None = Field(None, description="Custom headers")
    enabled: bool = Field(..., description="Whether schedule is active")
    created_at: datetime = Field(..., description="Creation timestamp")
    next_run_time: datetime | None = Field(
        None,
        description="Next scheduled execution time (None if paused/disabled)",
    )
    webhook_url: str | None = Field(
        default=None,
        description="Optional callback URL notified after each scheduled execution",
    )


class ScheduleListResponse(BaseModel):
    """Response model for listing schedules."""

    schedules: list[ScheduleResponse] = Field(..., description="List of schedules")
    total: int = Field(..., description="Total number of schedules")


class ScheduleExecution(BaseModel):
    """Record of a single scheduled job execution."""

    execution_id: str = Field(..., description="Unique execution identifier (UUID)")
    schedule_id: str = Field(..., description="ID of the parent schedule")
    status: ExecutionStatus = Field(..., description="Execution status")
    started_at: datetime = Field(..., description="Execution start time")
    completed_at: datetime | None = Field(None, description="Execution completion time")
    duration: float | None = Field(None, description="Execution duration in seconds")
    success: bool = Field(..., description="Whether execution succeeded")
    content_size: int | None = Field(None, description="Downloaded content size in bytes")
    error_message: str | None = Field(None, description="Error message if failed")
    attempt: int = Field(..., ge=1, description="Attempt number (1-based)")


class ScheduleExecutionListResponse(BaseModel):
    """Response model for listing schedule executions."""

    executions: list[ScheduleExecution] = Field(..., description="List of executions")
    total: int = Field(..., description="Total number of executions")
    limit: int = Field(..., description="Maximum results per page")
    offset: int = Field(..., description="Number of results skipped")

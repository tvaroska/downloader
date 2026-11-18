"""Pydantic models for schedule CRUD operations."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from apscheduler.triggers.cron import CronTrigger

_UNIX_DOW_NAMES: dict[str, str] = {
    "0": "sun",
    "1": "mon",
    "2": "tue",
    "3": "wed",
    "4": "thu",
    "5": "fri",
    "6": "sat",
    "7": "sun",
}


def _translate_unix_dow_item(item: str) -> str:
    """Translate one comma-separated UNIX day-of-week token (0/7=sun..6=sat) to APScheduler names."""
    base, sep, step = item.partition("/")
    result = item
    if "-" in base:
        start, _, end = base.partition("-")
        if start in _UNIX_DOW_NAMES and end in _UNIX_DOW_NAMES:
            s_idx, e_idx = int(start), int(end)
            if sep and step.isdigit() and int(step) > 0 and s_idx <= e_idx:
                days = [_UNIX_DOW_NAMES[str(i)] for i in range(s_idx, e_idx + 1, int(step))]
                result = ",".join(days)
            elif start == "0" and end in {"1", "2", "3", "4", "5", "6"}:
                result = "sun,mon" if end == "1" else f"sun,mon-{_UNIX_DOW_NAMES[end]}"
            else:
                result = f"{_UNIX_DOW_NAMES[start]}-{_UNIX_DOW_NAMES[end]}{sep}{step}"
    elif base in _UNIX_DOW_NAMES:
        result = f"{_UNIX_DOW_NAMES[base]}{sep}{step}"
    return result


def cron_trigger_from_unix_crontab(expr: str) -> CronTrigger:
    """Build an APScheduler `CronTrigger` from a 5-field UNIX crontab (0=Sunday, 1=Monday..6=Saturday)."""
    from apscheduler.triggers.cron import CronTrigger

    parts = expr.split()
    if len(parts) != 5:
        return CronTrigger.from_crontab(expr)
    translated_dow = ",".join(_translate_unix_dow_item(item) for item in parts[4].split(","))
    translated_expr = f"{parts[0]} {parts[1]} {parts[2]} {parts[3]} {translated_dow}"
    return CronTrigger.from_crontab(translated_expr)


class ExecutionStatus(str, Enum):
    """Status of a scheduled job execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ScheduleCreate(BaseModel):
    """Request model for creating a scheduled download job."""

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

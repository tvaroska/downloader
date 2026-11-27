"""Pydantic models for schedule CRUD operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

_UNIX_DOW_NAMES: tuple[str, ...] = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
_NAME_TO_DOW_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(_UNIX_DOW_NAMES)}
_MONTH_NAMES: tuple[str, ...] = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
_NAME_TO_MONTH: dict[str, int] = {name: idx + 1 for idx, name in enumerate(_MONTH_NAMES)}
_MAX_DAYS_PER_MONTH: dict[int, int] = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}
_RESERVED_SECRET_PREFIX = "enc:v1:"


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


@dataclass(frozen=True, slots=True)
class _CronFieldSpec:
    name: str
    min_val: int
    max_val: int
    names: dict[str, int] | None = None
    allow_last_dom: bool = False


_MINUTE_SPEC = _CronFieldSpec("minute", 0, 59)
_HOUR_SPEC = _CronFieldSpec("hour", 0, 23)
_DOM_SPEC = _CronFieldSpec("day", 1, 31, allow_last_dom=True)
_MONTH_SPEC = _CronFieldSpec("month", 1, 12, names=_NAME_TO_MONTH)


def _parse_bounded_int(token: str, spec: _CronFieldSpec) -> int:
    cleaned = token.strip().lower()
    if spec.names is not None and cleaned in spec.names:
        return spec.names[cleaned]
    if cleaned.isdigit():
        val = int(cleaned)
        if spec.min_val <= val <= spec.max_val:
            return val
    raise ValueError(f"Invalid {spec.name} value: {token!r}")


def _parse_cron_step(parts: list[str], spec: _CronFieldSpec, item: str) -> int:
    if len(parts) > 2:
        raise ValueError(f"Invalid {spec.name} step expression: {item!r}")
    if len(parts) == 1:
        return 1
    step_str = parts[1].strip()
    if not step_str.isdigit() or int(step_str) <= 0:
        raise ValueError(f"Invalid {spec.name} step value: {item!r}")
    step = int(step_str)
    max_step = spec.max_val - spec.min_val
    if max_step > 0 and step > max_step:
        raise ValueError(f"Step {step} exceeds range for {spec.name}")
    return step


def _validate_cron_item(item: str, spec: _CronFieldSpec) -> set[int]:
    token = item.strip()
    if not token:
        raise ValueError(f"Empty {spec.name} token")
    if spec.allow_last_dom and token.lower() == "last":
        return {28}

    parts = token.split("/")
    step = _parse_cron_step(parts, spec, item)
    base = parts[0].strip()
    if base == "*":
        return set(range(spec.min_val, spec.max_val + 1, step))
    if "-" in base:
        bounds = base.split("-")
        if len(bounds) != 2:
            raise ValueError(f"Invalid {spec.name} range: {item!r}")
        lo = _parse_bounded_int(bounds[0], spec)
        hi = _parse_bounded_int(bounds[1], spec)
        if lo > hi:
            raise ValueError(f"Invalid {spec.name} range: {item!r}")
        return set(range(lo, hi + 1, step))

    val = _parse_bounded_int(base, spec)
    return set(range(val, spec.max_val + 1, step)) if len(parts) == 2 else {val}


def _validate_cron_field(raw_field: str, spec: _CronFieldSpec) -> set[int]:
    values: set[int] = set()
    for item in raw_field.strip().split(","):
        values.update(_validate_cron_item(item, spec))
    return values


def validate_unix_crontab(expr: str) -> tuple[str, str, str, str, str]:
    """Validate a 5-field UNIX crontab expression in pure Python and return its 5 fields."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Wrong number of fields; got {len(parts)}, expected 5")

    minute, hour, dom, month, dow = parts
    _validate_cron_field(minute, _MINUTE_SPEC)
    _validate_cron_field(hour, _HOUR_SPEC)
    days = _validate_cron_field(dom, _DOM_SPEC)
    months = _validate_cron_field(month, _MONTH_SPEC)
    _translate_unix_dow_field(dow)

    max_month_day = max(_MAX_DAYS_PER_MONTH[m] for m in months)
    if dom != "*" and all(d > max_month_day for d in days):
        raise ValueError(f"Day-of-month {dom!r} is out of range for month {month!r}")
    return minute, hour, dom, month, dow


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
        """Validate cron expression using pure-Python UNIX 0=Sunday semantics."""
        try:
            validate_unix_crontab(v)
        except ValueError as e:
            raise ValueError(f"Invalid cron expression: {e}") from e
        return v

    @field_validator("webhook_secret")
    @classmethod
    def validate_webhook_secret(cls, v: str | None) -> str | None:
        """Reject caller-supplied ciphertext tokens starting with `enc:v1:`."""
        if v is not None and v.startswith(_RESERVED_SECRET_PREFIX):
            raise ValueError("webhook_secret must not start with reserved 'enc:v1:' prefix")
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


def __getattr__(name: str) -> Any:
    if name == "cron_trigger_from_unix_crontab":
        from ..scheduler.cron import cron_trigger_from_unix_crontab

        return cron_trigger_from_unix_crontab
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

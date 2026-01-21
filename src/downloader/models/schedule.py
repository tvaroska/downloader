"""Pydantic models for schedule CRUD operations."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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
    format: Literal["text", "html", "markdown", "pdf", "json", "raw"] = Field(
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

    @field_validator("cron_expression")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        """Validate cron expression using APScheduler."""
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(v)
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


class ScheduleListResponse(BaseModel):
    """Response model for listing schedules."""

    schedules: list[ScheduleResponse] = Field(..., description="List of schedules")
    total: int = Field(..., description="Total number of schedules")

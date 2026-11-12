"""Pydantic request and response models for the Downloader Python SDK."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BrowserActionType = Literal[
    "click",
    "fill",
    "select",
    "press",
    "hover",
    "scroll",
    "wait_for_selector",
    "wait",
]


class BrowserAction(BaseModel):
    """Declarative browser step executed prior to capture."""

    type: BrowserActionType
    selector: str | None = None
    value: str | None = None
    key: str | None = None
    x: int = 0
    y: int = 0
    timeout_ms: int = 5000
    duration_ms: int = 500


class DownloadResult(BaseModel):
    """Single URL download, render, OCR, PDF, or screenshot response."""

    url: str
    status_code: int
    content_type: str
    content: bytes
    headers: dict[str, str] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def rendered_with_js(self) -> bool:
        return self.headers.get("x-rendered-with-js", "").lower() == "true"

    @property
    def ocr_applied(self) -> bool:
        return self.headers.get("x-ocr-applied", "").lower() == "true"

    @property
    def trace_id(self) -> str | None:
        return self.headers.get("x-trace-id")


class BatchURLItem(BaseModel):
    """Single URL entry inside a batch submission."""

    url: str
    format: str | None = None
    custom_headers: dict[str, str] | None = None
    actions: list[BrowserAction] | None = None
    full_page: bool = False
    screenshot_format: Literal["png", "jpeg"] = "png"
    ocr_language: str | None = None


class BatchJobSubmission(BaseModel):
    """Response from POST /batch."""

    job_id: str
    status: str
    created_at: str
    total_urls: int
    estimated_completion: str | None = None


class BatchJobStatus(BaseModel):
    """Response from GET /jobs/{job_id}/status."""

    job_id: str
    status: str
    progress: int
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    total_urls: int
    processed_urls: int
    successful_urls: int
    failed_urls: int
    error_message: str | None = None
    results_available: bool
    expires_at: str | None = None


class BatchJobResults(BaseModel):
    """Response from GET /jobs/{job_id}/results."""

    job_id: str
    status: str
    total_duration: float
    results: list[dict[str, Any]]
    summary: dict[str, Any]
    created_at: str
    completed_at: str


class ScheduleCreateRequest(BaseModel):
    """Payload for POST /schedules."""

    name: str
    url: str
    cron_expression: str
    format: Literal["text", "html", "markdown", "pdf", "screenshot", "json", "raw"] = "text"
    headers: dict[str, str] | None = None
    enabled: bool = True
    webhook_url: str | None = None
    webhook_secret: str | None = None


class ScheduleInfo(BaseModel):
    """Schedule resource returned by schedule endpoints."""

    id: str
    name: str
    url: str
    cron_expression: str
    format: str
    headers: dict[str, str] | None = None
    enabled: bool
    created_at: str
    next_run_time: str | None = None
    webhook_url: str | None = None


class ScheduleExecution(BaseModel):
    """Single execution entry returned by GET /schedules/{id}/history."""

    execution_id: str
    schedule_id: str
    status: str
    started_at: str
    completed_at: str | None = None
    duration: float | None = None
    success: bool
    content_size: int | None = None
    error_message: str | None = None
    attempt: int


class HealthStatus(BaseModel):
    """Response from GET /health."""

    status: str
    version: str
    environment: str
    draining: bool = False
    active_requests: int = 0
    deployment_mode: str = "all"
    telemetry_enabled: bool = False
    configuration: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)

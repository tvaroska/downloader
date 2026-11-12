"""Runtime, feature, and operations configuration schemas added in v0.7.0."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OCRConfig(BaseSettings):
    """Image-to-text OCR configuration (pytesseract + Pillow)."""

    enabled: bool = Field(default=True, description="Enable OCR extraction for image/* responses")
    default_language: str = Field(
        default="eng",
        pattern=r"^[a-zA-Z0-9_+]{1,64}$",
        description="Default Tesseract OCR language code(s) (e.g., 'eng' or 'eng+deu')",
    )
    timeout: float = Field(
        default=15.0,
        ge=1.0,
        le=120.0,
        description="Maximum time in seconds for a single Tesseract OCR pass",
    )
    max_image_pixels: int = Field(
        default=25_000_000,
        ge=100_000,
        le=200_000_000,
        description="Pillow pixel cap to guard against decompression bombs",
    )

    model_config = SettingsConfigDict(env_prefix="OCR_")


class WebhookConfig(BaseSettings):
    """Outbound webhook delivery configuration for batch and scheduled jobs."""

    enabled: bool = Field(default=True, description="Enable outbound webhook delivery")
    timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        description="HTTP request timeout in seconds for each webhook delivery attempt",
    )
    max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum delivery attempts on transient 5xx/429/network errors",
    )
    backoff_base: float = Field(
        default=1.0,
        ge=0.0,
        le=30.0,
        description="Base exponential backoff delay in seconds between retries",
    )

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_")


class ShutdownConfig(BaseSettings):
    """Graceful shutdown and connection draining configuration."""

    drain_timeout: float = Field(
        default=30.0,
        ge=0.0,
        le=300.0,
        description="Maximum seconds to wait for active requests and jobs during shutdown",
    )

    model_config = SettingsConfigDict(env_prefix="SHUTDOWN_")


class WorkerConfig(BaseSettings):
    """Split worker deployment configuration."""

    deployment_mode: Literal["all", "api", "worker"] = Field(
        default="all",
        alias="DEPLOYMENT_MODE",
        description="Runtime role: 'all' (monolith), 'api' (stateless HTTP), or 'worker' (queue + scheduler + browser)",
    )
    poll_timeout: float = Field(
        default=2.0,
        ge=0.2,
        le=30.0,
        description="Blocking Redis pop timeout in seconds for worker loops",
    )
    heartbeat_interval: float = Field(
        default=10.0,
        ge=1.0,
        le=120.0,
        description="Seconds between worker Redis heartbeat refreshes",
    )
    heartbeat_ttl: int = Field(
        default=30,
        ge=5,
        le=300,
        description="TTL in seconds for worker heartbeat keys in Redis",
    )
    max_concurrent_jobs: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Maximum concurrent batch jobs processed by a single worker process",
    )
    remote_browser_enabled: bool = Field(
        default=False,
        description="When DEPLOYMENT_MODE=api, offload Playwright tasks to worker pods via Redis queue",
    )
    remote_browser_timeout: float = Field(
        default=45.0,
        ge=5.0,
        le=180.0,
        description="Timeout in seconds awaiting a remote worker browser response",
    )

    model_config = SettingsConfigDict(env_prefix="WORKER_", populate_by_name=True)


class TelemetryConfig(BaseSettings):
    """OpenTelemetry distributed tracing configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable OpenTelemetry tracing instrumentation and span export",
    )
    service_name: str = Field(
        default="downloader",
        description="OpenTelemetry service.name resource attribute",
    )
    exporter_otlp_endpoint: str | None = Field(
        default=None,
        description="OTLP HTTP trace endpoint (e.g., http://otel-collector:4318/v1/traces)",
    )
    traces_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Head-based trace sampling ratio (0.0 to 1.0)",
    )
    console_exporter: bool = Field(
        default=False,
        description="Also emit spans to stdout via ConsoleSpanExporter (useful for local debugging)",
    )

    model_config = SettingsConfigDict(env_prefix="OTEL_")

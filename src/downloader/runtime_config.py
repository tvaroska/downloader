"""Runtime, feature, and operations configuration schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, model_validator
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
        validation_alias=AliasChoices("OCR_TIMEOUT", "OCR_TIMEOUT_SECONDS"),
        description="Maximum time in seconds for a single Tesseract OCR pass",
    )
    max_image_pixels: int = Field(
        default=25_000_000,
        ge=100_000,
        le=200_000_000,
        validation_alias=AliasChoices("OCR_MAX_IMAGE_PIXELS", "OCR_MAX_PIXELS"),
        description="Pillow pixel cap to guard against decompression bombs",
    )

    model_config = SettingsConfigDict(env_prefix="OCR_", populate_by_name=True, extra="ignore")


class WebhookConfig(BaseSettings):
    """Outbound webhook delivery configuration for batch and scheduled jobs."""

    enabled: bool = Field(default=True, description="Enable outbound webhook delivery")
    timeout: float = Field(
        default=10.0,
        ge=1.0,
        le=60.0,
        validation_alias=AliasChoices("WEBHOOK_TIMEOUT", "WEBHOOK_TIMEOUT_SECONDS"),
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
    secret_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEBHOOK_SECRET_KEY", "WEBHOOK_ENCRYPTION_KEY"),
        description="Optional symmetric key encrypting webhook_secret at rest in Redis (falls back to DOWNLOADER_KEY)",
    )

    model_config = SettingsConfigDict(env_prefix="WEBHOOK_", populate_by_name=True, extra="ignore")


class ShutdownConfig(BaseSettings):
    """Graceful shutdown and connection draining configuration."""

    drain_timeout: float = Field(
        default=30.0,
        ge=0.0,
        le=300.0,
        description="Maximum seconds to wait for active requests and jobs during shutdown",
    )
    pre_stop_delay: float = Field(
        default=2.0,
        ge=0.0,
        le=60.0,
        description="Seconds after SIGTERM to hold /health at 503 before closing listening sockets",
    )

    model_config = SettingsConfigDict(env_prefix="SHUTDOWN_", extra="ignore")


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
        default=60,
        ge=5,
        le=600,
        description="TTL in seconds for worker heartbeat keys in Redis (up to drain_timeout 300s + heartbeat_interval 120s)",
    )
    max_concurrent_jobs: int = Field(
        default=4,
        ge=1,
        le=64,
        description="Maximum concurrent batch jobs processed by a single worker process",
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum worker delivery attempts for a batch job before marking it failed",
    )
    remote_browser_enabled: bool = Field(
        default=False,
        description="When DEPLOYMENT_MODE=api, offload Playwright tasks to worker pods via Redis queue",
    )
    remote_browser_timeout: float = Field(
        default=45.0,
        ge=5.0,
        le=180.0,
        validation_alias=AliasChoices(
            "WORKER_REMOTE_BROWSER_TIMEOUT", "WORKER_BROWSER_TASK_TIMEOUT"
        ),
        description="Timeout in seconds awaiting a remote worker browser response",
    )
    max_remote_browser_calls: int = Field(
        default=8,
        ge=1,
        le=256,
        validation_alias=AliasChoices(
            "WORKER_MAX_REMOTE_BROWSER_CALLS", "WORKER_REMOTE_BROWSER_CONCURRENCY"
        ),
        description="Maximum concurrent in-flight remote browser RPC calls per API process",
    )
    metrics_port: int | None = Field(
        default=None,
        ge=0,
        le=65535,
        description="Optional TCP port exposing Prometheus /metrics for the standalone worker process",
    )

    model_config = SettingsConfigDict(env_prefix="WORKER_", populate_by_name=True, extra="ignore")

    @model_validator(mode="after")
    def validate_heartbeat_ttl_exceeds_interval(self) -> WorkerConfig:
        if self.heartbeat_ttl <= self.heartbeat_interval:
            raise ValueError(
                f"heartbeat_ttl ({self.heartbeat_ttl}s) must be greater than "
                f"heartbeat_interval ({self.heartbeat_interval}s)"
            )
        return self


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
        description="OTLP HTTP collector base URL or /v1/traces endpoint (e.g., http://otel-collector:4318)",
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

    model_config = SettingsConfigDict(env_prefix="OTEL_", extra="ignore")

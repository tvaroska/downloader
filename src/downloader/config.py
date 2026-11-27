"""Centralized configuration management using Pydantic Settings."""

from __future__ import annotations

import math
import multiprocessing
from typing import Annotated, Any, Literal

import limits
from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from .runtime_config import (
    OCRConfig as OCRConfig,
)
from .runtime_config import (
    ShutdownConfig as ShutdownConfig,
)
from .runtime_config import (
    TelemetryConfig as TelemetryConfig,
)
from .runtime_config import (
    WebhookConfig as WebhookConfig,
)
from .runtime_config import (
    WorkerConfig as WorkerConfig,
)


class HTTPClientConfig(BaseSettings):
    """HTTP client connection pool and timeout settings."""

    max_keepalive_connections: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Number of connections to keep alive in the pool",
    )
    max_connections: int = Field(
        default=200,
        ge=20,
        le=1000,
        description="Maximum total connections (should be >= 2x keepalive)",
    )
    keepalive_expiry: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Seconds to keep idle connections alive",
    )
    request_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Default timeout for HTTP requests in seconds",
    )
    max_redirects: int = Field(
        default=10,
        ge=0,
        le=50,
        description="Maximum number of redirects to follow",
    )
    http2_enabled: bool = Field(
        default=True,
        description="Enable HTTP/2 support for better multiplexing",
    )

    model_config = SettingsConfigDict(env_prefix="HTTP_", extra="ignore")

    @field_validator("max_connections")
    @classmethod
    def validate_max_connections(cls, v: int, info: Any) -> int:
        """Ensure max_connections >= max_keepalive_connections."""
        if "max_keepalive_connections" in info.data:
            keepalive = info.data["max_keepalive_connections"]
            if v < keepalive:
                raise ValueError(
                    f"max_connections ({v}) must be >= max_keepalive_connections ({keepalive})"
                )
        return v


def _default_batch_concurrency() -> int:
    return min(multiprocessing.cpu_count() * 8, 50)


class PDFConfig(BaseSettings):
    """Playwright browser pool and PDF/screenshot concurrency settings."""

    concurrency: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Max concurrent browser sessions (defaults to pool_size, must be <= pool_size)",
    )
    page_load_timeout: int = Field(
        default=30000,
        ge=1000,
        le=120000,
        description="Playwright page load timeout in milliseconds",
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="domcontentloaded", description="Playwright wait_until strategy"
    )
    pool_size: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of browser instances to maintain in the pool",
    )
    browser_memory_limit_mb: int = Field(
        default=512,
        ge=128,
        le=4096,
        description="Memory limit per browser instance in MB",
    )
    max_uses_per_browser: int = Field(
        default=50,
        ge=1,
        le=10000,
        description="Recycle browser instance after this many context uses",
    )
    idle_timeout: float = Field(
        default=600.0,
        ge=0.0,
        le=86400.0,
        description="Seconds of inactivity before closing idle browser pool (0 disables scale-to-zero)",
    )

    model_config = SettingsConfigDict(env_prefix="PDF_", extra="ignore")

    @model_validator(mode="after")
    def _sync_concurrency_with_pool_size(self) -> PDFConfig:
        if "concurrency" not in self.model_fields_set:
            self.concurrency = self.pool_size
        elif self.concurrency > self.pool_size:
            raise ValueError(
                f"PDF_CONCURRENCY ({self.concurrency}) must not exceed PDF_POOL_SIZE ({self.pool_size})"
            )
        return self


class BatchConfig(BaseSettings):
    """Batch processing concurrency and timeout settings."""

    concurrency: int = Field(
        default_factory=_default_batch_concurrency,
        ge=1,
        le=100,
        description="Max concurrent batch requests (default: 8x CPU cores, max 50)",
    )
    max_urls_per_batch: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum number of URLs allowed in a single batch request",
    )
    default_timeout_per_url: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Default timeout per URL in batch processing (seconds)",
    )

    model_config = SettingsConfigDict(env_prefix="BATCH_", extra="ignore")


class ContentConfig(BaseSettings):
    """Content download size cap and JS-rendering detection settings."""

    max_download_size: int = Field(
        default=50 * 1024 * 1024,
        ge=1024 * 1024,
        le=500 * 1024 * 1024,
        description="Maximum download and rendered output size in bytes (default: 50MB)",
    )
    cache_max_size: int = Field(
        default=1000,
        ge=0,
        le=10000,
        description="Maximum number of entries in content conversion cache",
    )
    enable_html_js_rendering: bool = Field(
        default=True,
        description="Enable Playwright rendering for JavaScript-heavy HTML pages",
    )
    html_js_rendering_timeout: int = Field(
        default=10000,
        ge=1000,
        le=60000,
        description="Playwright rendering timeout in milliseconds (default: 10s)",
    )
    html_js_rendering_cache_ttl: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="TTL for HTML rendering detection cache in seconds (default: 1 hour)",
    )
    html_js_heavy_domains: Annotated[list[str], NoDecode] = Field(
        default=["substack.com", "medium.com", "notion.so", "ghost.io"],
        description="Known JS-heavy domains that always need rendering",
    )
    min_js_framework_content_threshold: int = Field(
        default=200,
        ge=50,
        le=2000,
        description="Minimum body text chars for JS framework detection (default: 200)",
    )

    model_config = SettingsConfigDict(env_prefix="CONTENT_", extra="ignore")

    @field_validator("html_js_heavy_domains", mode="before")
    @classmethod
    def parse_domains(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [domain.strip() for domain in v.split(",") if domain.strip()]
        return v


class RedisConfig(BaseSettings):
    """Redis configuration for job management."""

    redis_uri: str | None = Field(
        default=None,
        alias="REDIS_URI",
        description="Redis connection URI (e.g., redis://localhost:6379)",
    )
    max_connections: int = Field(
        default=10,
        ge=1,
        le=100,
        alias="REDIS_MAX_CONNECTIONS",
        description="Maximum Redis connections in pool",
    )

    model_config = SettingsConfigDict(env_prefix="REDIS_", populate_by_name=True, extra="ignore")


class SchedulerConfig(BaseSettings):
    """APScheduler configuration for recurring download jobs."""

    job_store_type: Literal["memory", "redis"] = Field(
        default="redis",
        description="Job store backend type (redis requires REDIS_URI)",
    )
    max_workers: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum number of concurrent scheduled job workers",
    )
    misfire_grace_time: int = Field(
        default=60,
        ge=1,
        le=3600,
        description="Seconds a job can be late before considered misfired",
    )
    coalesce: bool = Field(
        default=True,
        description="Combine multiple missed runs into a single execution",
    )

    model_config = SettingsConfigDict(env_prefix="SCHEDULER_", extra="ignore")


class AuthConfig(BaseSettings):
    """API-key authentication configuration."""

    api_key: str | None = Field(
        default=None,
        alias="DOWNLOADER_KEY",
        description="API key for authentication (if None, auth is disabled)",
    )
    allow_unauthenticated_production: bool = Field(
        default=False,
        alias="AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION",
        description="Explicitly allow running in production without DOWNLOADER_KEY (unsafe)",
    )

    model_config = SettingsConfigDict(
        case_sensitive=True,
        populate_by_name=True,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("api_key", mode="before")
    @classmethod
    def _strip_blank_api_key(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip() or None
        return v


class LoggingConfig(BaseSettings):
    """Structured and rotating file logging configuration."""

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        validation_alias=AliasChoices("LOG_LEVEL", "LOG_LOG_LEVEL"),
        description="Application log level",
    )
    access_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO", description="Uvicorn access log level"
    )
    json_logs: bool = Field(
        default=False,
        description="Enable JSON formatted logs (recommended for production)",
    )
    access_log_file: str | None = Field(
        default=None, description="Path to access log file (None = stdout)"
    )
    error_log_file: str | None = Field(
        default=None, description="Path to error log file (None = stderr)"
    )
    log_rotation_size: int = Field(
        default=10 * 1024 * 1024,
        ge=1024 * 1024,
        validation_alias=AliasChoices("LOG_ROTATION_SIZE", "LOG_LOG_ROTATION_SIZE"),
        description="Log file size before rotation (bytes)",
    )
    log_rotation_count: int = Field(
        default=5,
        ge=1,
        le=100,
        validation_alias=AliasChoices("LOG_ROTATION_COUNT", "LOG_LOG_ROTATION_COUNT"),
        description="Number of rotated log files to keep",
    )

    model_config = SettingsConfigDict(env_prefix="LOG_", populate_by_name=True, extra="ignore")


class SSRFConfig(BaseSettings):
    """SSRF protection configuration."""

    block_private_ips: bool = Field(
        default=True, description="Block requests to private IP addresses"
    )
    block_cloud_metadata: bool = Field(
        default=True,
        description="Block requests to cloud metadata endpoints (169.254.169.254)",
    )
    resolve_dns: bool = Field(
        default=True,
        description="Resolve DNS and check IPs before making requests",
    )
    allow_unresolved_dns_production: bool = Field(
        default=False,
        alias="SSRF_ALLOW_UNRESOLVED_DNS_PRODUCTION",
        description="Explicitly allow SSRF_RESOLVE_DNS=false when ENVIRONMENT=production (unsafe)",
    )

    model_config = SettingsConfigDict(env_prefix="SSRF_", populate_by_name=True, extra="ignore")


class CORSConfig(BaseSettings):
    """CORS configuration defaulting to localhost origins."""

    allowed_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:3000",
            "http://localhost:8080",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8080",
        ],
        description="Allowed CORS origins. Default: localhost only. Set explicitly in production.",
    )

    model_config = SettingsConfigDict(env_prefix="CORS_", extra="ignore")

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v


class RateLimitConfig(BaseSettings):
    """Moving-window rate-limiting configuration."""

    enabled: bool = Field(
        default=True,
        description="Enable rate limiting (disable only for testing)",
    )
    default_limit: str = Field(
        default="100/minute",
        description="Default rate limit for all endpoints (format: count/period)",
    )
    download_limit: str = Field(
        default="60/minute",
        description="Rate limit for direct download endpoints",
    )
    batch_limit: str = Field(
        default="20/minute",
        description="Rate limit for batch job creation",
    )
    status_limit: str = Field(
        default="200/minute",
        description="Rate limit for status/metrics endpoints",
    )
    storage_uri: str | None = Field(
        default=None,
        description="Redis URI for distributed rate limiting (uses REDIS_URI if None)",
    )
    headers_enabled: bool = Field(
        default=True,
        description="Include rate limit headers in responses (X-RateLimit-*)",
    )
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default=[
            "127.0.0.1",
            "::1",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
        ],
        description="Trusted reverse-proxy IPs or CIDRs allowed to supply X-Forwarded-For",
    )

    model_config = SettingsConfigDict(env_prefix="RATELIMIT_", extra="ignore")

    @field_validator("default_limit", "download_limit", "batch_limit", "status_limit")
    @classmethod
    def validate_rate_limit_syntax(cls, v: str) -> str:
        try:
            limits.parse(v)
        except ValueError as exc:
            raise ValueError(f"Invalid rate limit expression {v!r}: {exc}") from exc
        return v

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def parse_trusted_proxies(cls, v: Any) -> Any:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


class CrawlingConfig(BaseSettings):
    """Ethical crawling configuration for robots.txt and Crawl-delay enforcement."""

    user_agent: str = Field(
        default="REST-API-Downloader/1.0",
        alias="USER_AGENT",
        description="User-Agent header sent for HTTP requests, robots.txt fetches, and browser contexts",
    )
    respect_robots_txt: bool = Field(
        default=True,
        alias="RESPECT_ROBOTS_TXT",
        description="Evaluate origin robots.txt rules and Crawl-delay before downloading",
    )
    robots_cache_ttl: int = Field(
        default=3600,
        ge=60,
        le=86400,
        alias="ROBOTS_CACHE_TTL",
        description="TTL in seconds for cached robots.txt rules",
    )
    robots_cache_max_size: int = Field(
        default=1000,
        ge=10,
        le=50000,
        alias="ROBOTS_CACHE_MAX_SIZE",
        description="Maximum number of origins stored in the in-memory LRU robots.txt cache",
    )
    robots_fetch_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        alias="ROBOTS_FETCH_TIMEOUT",
        description="Timeout in seconds when fetching origin robots.txt",
    )
    max_crawl_delay: float = Field(
        default=30.0,
        ge=0.0,
        le=300.0,
        alias="MAX_CRAWL_DELAY",
        description="Upper bound in seconds applied to origin Crawl-delay directives",
    )

    model_config = SettingsConfigDict(
        populate_by_name=True,
        case_sensitive=False,
        extra="ignore",
    )


_ENV_FILE_UNSET: Any = object()


class Settings(BaseSettings):
    """Main application settings combining all configuration sections."""

    app_name: str = Field(default="REST API Downloader", description="Application name")
    environment: Literal["development", "staging", "production"] = Field(
        default="development", description="Runtime environment"
    )

    http: HTTPClientConfig = Field(default_factory=HTTPClientConfig)
    pdf: PDFConfig = Field(default_factory=PDFConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ssrf: SSRFConfig = Field(default_factory=SSRFConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    ratelimit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    crawling: CrawlingConfig = Field(default_factory=CrawlingConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    webhook: WebhookConfig = Field(default_factory=WebhookConfig)
    shutdown: ShutdownConfig = Field(default_factory=ShutdownConfig)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def __init__(
        self,
        _env_file: Any = _ENV_FILE_UNSET,
        _env_file_encoding: str | None = None,
        **values: Any,
    ) -> None:
        effective_env_file = (
            self.model_config.get("env_file") if _env_file is _ENV_FILE_UNSET else _env_file
        )
        effective_encoding = (
            _env_file_encoding
            if _env_file_encoding is not None
            else self.model_config.get("env_file_encoding", "utf-8")
        )
        for field_name, field_info in type(self).model_fields.items():
            if field_name in values:
                continue
            ann = field_info.annotation
            if isinstance(ann, type) and issubclass(ann, BaseSettings):
                values[field_name] = ann(
                    _env_file=effective_env_file,
                    _env_file_encoding=effective_encoding,
                )
        super().__init__(
            _env_file=effective_env_file,
            _env_file_encoding=effective_encoding,
            **values,
        )

    @model_validator(mode="after")
    def validate_production_auth(self) -> Settings:
        """Refuse to start in production without a non-empty API key unless explicitly opted out."""
        if self.environment == "production":
            has_key = bool(self.auth.api_key and self.auth.api_key.strip())
            if not has_key and not self.auth.allow_unauthenticated_production:
                raise ValueError(
                    "DOWNLOADER_KEY must be set to a non-empty value when ENVIRONMENT=production "
                    "(or set AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION=true to explicitly opt out)."
                )
        return self

    @model_validator(mode="after")
    def validate_production_ssrf(self) -> Settings:
        """Refuse to start in production with DNS SSRF resolution disabled unless explicitly opted out."""
        if (
            self.environment == "production"
            and not self.ssrf.resolve_dns
            and not self.ssrf.allow_unresolved_dns_production
        ):
            raise ValueError(
                "SSRF_RESOLVE_DNS=false is not allowed when ENVIRONMENT=production "
                "(set SSRF_ALLOW_UNRESOLVED_DNS_PRODUCTION=true to explicitly opt out)."
            )
        return self

    @model_validator(mode="after")
    def validate_worker_heartbeat_covers_drain(self) -> Settings:
        """Ensure worker heartbeat TTL covers shutdown drain plus one heartbeat interval (`R83`)."""
        min_ttl = self.shutdown.drain_timeout + self.worker.heartbeat_interval
        if "heartbeat_ttl" not in self.worker.model_fields_set:
            if self.worker.heartbeat_ttl < min_ttl:
                self.worker.heartbeat_ttl = math.ceil(min_ttl)
        elif self.worker.heartbeat_ttl < min_ttl:
            raise ValueError(
                f"worker.heartbeat_ttl ({self.worker.heartbeat_ttl}s) must be >= "
                f"shutdown.drain_timeout + worker.heartbeat_interval ({min_ttl:.1f}s)"
            )
        return self

    def _production_warnings(self) -> list[str]:
        warnings: list[str] = []
        if not self.auth.api_key:
            warnings.append("WARNING: No API key configured in production")
        if "*" in self.cors.allowed_origins:
            warnings.append("WARNING: CORS allows all origins in production - security risk!")
        elif not self.cors.allowed_origins:
            warnings.append("WARNING: CORS origins empty - cross-origin requests blocked")
        elif any(
            o.startswith(("http://localhost", "http://127.0.0.1"))
            for o in self.cors.allowed_origins
        ):
            warnings.append("WARNING: CORS includes localhost in production - likely misconfigured")
        if not self.logging.json_logs:
            warnings.append("INFO: JSON logs recommended for production")
        if not self.ssrf.resolve_dns:
            warnings.append("WARNING: DNS resolution disabled - SSRF risk increased")
        if not self.ratelimit.enabled:
            warnings.append("WARNING: Rate limiting disabled in production - DoS risk")
        if self.redis.redis_uri and self.webhook.enabled and not self.webhook.secret_key:
            warnings.append(
                "WARNING: REDIS_URI is set without WEBHOOK_SECRET_KEY - stored webhook secrets require a dedicated key"
            )
        return warnings

    def validate_settings(self) -> list[str]:
        """Validate settings and return startup diagnostic messages."""
        messages = self._production_warnings() if self.environment == "production" else []
        messages.extend(
            [
                f"INFO: PDF concurrency: {self.pdf.concurrency}",
                f"INFO: Batch concurrency: {self.batch.concurrency}",
                f"INFO: Max download size: {self.content.max_download_size / 1024 / 1024:.1f}MB",
                f"INFO: Redis: {'enabled' if self.redis.redis_uri else 'disabled'}",
                f"INFO: Auth: {'enabled' if self.auth.api_key else 'disabled'}",
                f"INFO: Rate limiting: {'enabled' if self.ratelimit.enabled else 'disabled'}",
                f"INFO: Deployment mode: {self.worker.deployment_mode}",
                f"INFO: OpenTelemetry: {'enabled' if self.telemetry.enabled else 'disabled'}",
            ]
        )
        return messages


_settings: Settings | None = None


def get_settings() -> Settings:
    """Get the global settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment."""
    global _settings
    _settings = Settings()
    return _settings

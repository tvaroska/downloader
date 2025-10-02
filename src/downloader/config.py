"""Centralized configuration management using Pydantic Settings.

This module provides a type-safe, validated configuration system for the entire application.
All environment variables and magic numbers are documented here with their rationale.
"""

import multiprocessing
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class HTTPClientConfig(BaseSettings):
    """HTTP client configuration settings.

    These settings control connection pooling, timeouts, and concurrency for HTTP requests.
    """

    # Connection Pool Settings
    # Why these numbers? Based on testing with high-concurrency workloads:
    # - 100 keepalive: Balances memory vs connection reuse for typical traffic
    # - 200 total: Allows 2x keepalive for burst traffic without running out of connections
    # - 30s keepalive_expiry: Standard for most HTTP servers, prevents stale connections
    max_keepalive_connections: int = Field(
        default=100,
        ge=10,
        le=500,
        description="Number of connections to keep alive in the pool"
    )
    max_connections: int = Field(
        default=200,
        ge=20,
        le=1000,
        description="Maximum total connections (should be >= 2x keepalive)"
    )
    keepalive_expiry: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Seconds to keep idle connections alive"
    )

    # Request Settings
    # Why 30s? Most web pages load within 10-15s; 30s provides safety margin for slow servers
    # Why 10 redirects? Standard browser behavior (Chrome, Firefox use 10-20)
    request_timeout: float = Field(
        default=30.0,
        ge=5.0,
        le=300.0,
        description="Default timeout for HTTP requests in seconds"
    )
    max_redirects: int = Field(
        default=10,
        ge=0,
        le=50,
        description="Maximum number of redirects to follow"
    )

    # Concurrency Settings
    # Why 20 for API? Balances responsiveness vs server load for synchronous requests
    # Why 5 for batch? Batch jobs are background tasks, limit to prevent resource exhaustion
    max_concurrent_api: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max concurrent HTTP requests for API endpoints"
    )
    max_concurrent_batch: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Max concurrent HTTP requests for batch processing"
    )

    # HTTP/2 Support
    http2_enabled: bool = Field(
        default=True,
        description="Enable HTTP/2 support for better multiplexing"
    )

    model_config = SettingsConfigDict(env_prefix="HTTP_")

    @field_validator("max_connections")
    @classmethod
    def validate_max_connections(cls, v, info):
        """Ensure max_connections >= max_keepalive_connections."""
        if "max_keepalive_connections" in info.data:
            keepalive = info.data["max_keepalive_connections"]
            if v < keepalive:
                raise ValueError(
                    f"max_connections ({v}) must be >= max_keepalive_connections ({keepalive})"
                )
        return v


class PDFConfig(BaseSettings):
    """PDF generation configuration settings.

    PDF generation is CPU and memory intensive, so conservative concurrency limits are used.
    """

    # Concurrency Settings
    # Why 2x CPU cores? PDF rendering is CPU-bound but has I/O wait during page loading
    # Why max 12? Playwright browsers use ~200-300MB RAM each; 12 ≈ 2.4-3.6GB max
    # This prevents memory exhaustion on smaller VMs while allowing scaling on larger ones
    def _get_default_concurrency():
        cpu_count = multiprocessing.cpu_count()
        return min(cpu_count * 2, 12)

    concurrency: int = Field(
        default_factory=_get_default_concurrency,
        ge=1,
        le=50,
        description="Max concurrent PDF generations (default: 2x CPU cores, max 12)"
    )

    # Playwright Settings
    # Why 10s? Most pages load within 5s; 10s is aggressive but prevents indefinite hangs
    # networkidle can hang on streaming sites, but provides best results when it works
    page_load_timeout: int = Field(
        default=10000,
        ge=1000,
        le=60000,
        description="Playwright page load timeout in milliseconds"
    )
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="networkidle",
        description="Playwright wait_until strategy"
    )

    # Browser Pool Settings
    # Why 3? Balances browser startup cost vs memory overhead
    # Each Chromium instance uses ~100MB base + ~50-100MB per page
    pool_size: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of browser instances to maintain in the pool"
    )

    model_config = SettingsConfigDict(env_prefix="PDF_")


class BatchConfig(BaseSettings):
    """Batch processing configuration settings.

    Batch processing is I/O bound, so more aggressive concurrency is acceptable.
    """

    # Concurrency Settings
    # Why 8x CPU cores? Batch requests are I/O bound (network waits), can handle more parallelism
    # Why max 50? Limits total memory usage and prevents overwhelming downstream services
    # At 50 concurrent downloads × ~10MB average = ~500MB max memory usage
    def _get_default_concurrency():
        cpu_count = multiprocessing.cpu_count()
        return min(cpu_count * 8, 50)

    concurrency: int = Field(
        default_factory=_get_default_concurrency,
        ge=1,
        le=100,
        description="Max concurrent batch requests (default: 8x CPU cores, max 50)"
    )

    # Batch Size Limits
    # Why 50? Balances API usability vs DoS protection
    # 50 URLs × 30s timeout × 10MB = potential 1.5GB memory, 25min processing
    max_urls_per_batch: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum number of URLs allowed in a single batch request"
    )

    # Timeout Settings
    # Why 30s? Same rationale as HTTP request_timeout, applied per URL in batch
    default_timeout_per_url: int = Field(
        default=30,
        ge=5,
        le=300,
        description="Default timeout per URL in batch processing (seconds)"
    )

    model_config = SettingsConfigDict(env_prefix="BATCH_")


class ContentConfig(BaseSettings):
    """Content processing configuration settings."""

    # Size Limits
    # Why 50MB? Balances usability vs memory safety
    # - Most web pages are <5MB; 50MB handles large documents
    # - At 50MB × 50 concurrent = 2.5GB max memory for downloads
    # - Prevents DoS via unlimited content downloads
    max_download_size: int = Field(
        default=50 * 1024 * 1024,  # 50MB in bytes
        ge=1024 * 1024,  # Min 1MB
        le=500 * 1024 * 1024,  # Max 500MB
        description="Maximum download size in bytes (default: 50MB)"
    )

    # Cache Settings
    # Why 1000? LRU cache for content conversion results
    # Typical cache entry: ~1KB × 1000 = ~1MB memory overhead
    # Why 3600s? Balance between cache hit rate and memory usage
    cache_max_size: int = Field(
        default=1000,
        ge=0,
        le=10000,
        description="Maximum number of entries in content conversion cache"
    )
    cache_cleanup_interval: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Interval in seconds between cache cleanup runs"
    )

    model_config = SettingsConfigDict(env_prefix="CONTENT_")


class RedisConfig(BaseSettings):
    """Redis configuration for job management."""

    redis_uri: str | None = Field(
        default=None,
        description="Redis connection URI (e.g., redis://localhost:6379)"
    )

    # Connection Pool Settings
    max_connections: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum Redis connections in pool"
    )

    model_config = SettingsConfigDict(env_prefix="REDIS_")


class AuthConfig(BaseSettings):
    """Authentication configuration."""

    api_key: str | None = Field(
        default=None,
        alias="DOWNLOADER_KEY",
        description="API key for authentication (if None, auth is disabled)"
    )

    model_config = SettingsConfigDict(case_sensitive=True)


class LoggingConfig(BaseSettings):
    """Logging configuration with structured logging support."""

    # Log Levels
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Application log level"
    )

    # Access Logging (separate from error logs)
    access_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Uvicorn access log level"
    )

    # Structured Logging
    json_logs: bool = Field(
        default=False,
        description="Enable JSON formatted logs (recommended for production)"
    )

    # Log Files
    access_log_file: str | None = Field(
        default=None,
        description="Path to access log file (None = stdout)"
    )
    error_log_file: str | None = Field(
        default=None,
        description="Path to error log file (None = stderr)"
    )

    # Log Rotation
    log_rotation_size: int = Field(
        default=10 * 1024 * 1024,  # 10MB
        ge=1024 * 1024,  # Min 1MB
        description="Log file size before rotation (bytes)"
    )
    log_rotation_count: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Number of rotated log files to keep"
    )

    model_config = SettingsConfigDict(env_prefix="LOG_")


class SSRFConfig(BaseSettings):
    """SSRF protection configuration."""

    # Private IP Protection
    block_private_ips: bool = Field(
        default=True,
        description="Block requests to private IP addresses"
    )

    # Cloud Metadata Protection
    block_cloud_metadata: bool = Field(
        default=True,
        description="Block requests to cloud metadata endpoints (169.254.169.254)"
    )

    # DNS Resolution
    resolve_dns: bool = Field(
        default=True,
        description="Resolve DNS and check IPs before making requests"
    )

    model_config = SettingsConfigDict(env_prefix="SSRF_")


class CORSConfig(BaseSettings):
    """CORS configuration."""

    # Why "*" default? Development convenience; should be restricted in production
    allowed_origins: list[str] = Field(
        default=["*"],
        description="Allowed CORS origins (use specific domains in production)"
    )

    model_config = SettingsConfigDict(env_prefix="CORS_")

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v):
        """Parse comma-separated origins from environment variable."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v


class Settings(BaseSettings):
    """Main application settings combining all configuration sections."""

    # Application Metadata
    app_name: str = Field(
        default="REST API Downloader",
        description="Application name"
    )
    app_version: str = Field(
        default="0.0.1",
        description="Application version"
    )

    # Environment
    environment: Literal["development", "staging", "production"] = Field(
        default="development",
        description="Runtime environment"
    )

    # Component Configurations
    http: HTTPClientConfig = Field(default_factory=HTTPClientConfig)
    pdf: PDFConfig = Field(default_factory=PDFConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ssrf: SSRFConfig = Field(default_factory=SSRFConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    def validate_settings(self) -> list[str]:
        """Validate settings and return list of warnings/info messages."""
        messages = []

        # Security warnings
        if self.environment == "production":
            if not self.auth.api_key:
                messages.append("WARNING: No API key configured in production")

            if "*" in self.cors.allowed_origins:
                messages.append("WARNING: CORS allows all origins in production")

            if not self.logging.json_logs:
                messages.append("INFO: JSON logs recommended for production")

            if not self.ssrf.resolve_dns:
                messages.append("WARNING: DNS resolution disabled - SSRF risk increased")

        # Configuration info
        messages.append(f"INFO: PDF concurrency: {self.pdf.concurrency}")
        messages.append(f"INFO: Batch concurrency: {self.batch.concurrency}")
        messages.append(f"INFO: Max download size: {self.content.max_download_size / 1024 / 1024:.1f}MB")
        messages.append(f"INFO: Redis: {'enabled' if self.redis.redis_uri else 'disabled'}")
        messages.append(f"INFO: Auth: {'enabled' if self.auth.api_key else 'disabled'}")

        return messages


# Global settings instance
_settings: Settings | None = None


def get_settings() -> Settings:
    """Get the global settings instance (singleton pattern)."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reload_settings() -> Settings:
    """Reload settings from environment (useful for testing)."""
    global _settings
    _settings = Settings()
    return _settings

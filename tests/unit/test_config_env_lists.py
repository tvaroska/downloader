"""Unit tests for comma-separated list env vars, `.env` propagation, and env aliases (`R35`, `R36`, `R53`)."""

from __future__ import annotations

from pathlib import Path

import pytest

from downloader.config import Settings


@pytest.mark.unit
def test_comma_separated_list_env_vars_parse(monkeypatch: pytest.MonkeyPatch) -> None:
    """`CORS_ALLOWED_ORIGINS`, `RATELIMIT_TRUSTED_PROXIES`, and `CONTENT_HTML_JS_HEAVY_DOMAINS` accept CSV."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://app.example.com, https://admin.example.com ,",
    )
    monkeypatch.setenv(
        "RATELIMIT_TRUSTED_PROXIES",
        "10.0.0.0/8, 192.168.1.10 , ::1",
    )
    monkeypatch.setenv(
        "CONTENT_HTML_JS_HEAVY_DOMAINS",
        "example.com, spa.example.org, ",
    )

    settings = Settings()
    assert settings.cors.allowed_origins == [
        "https://app.example.com",
        "https://admin.example.com",
    ]
    assert settings.ratelimit.trusted_proxies == [
        "10.0.0.0/8",
        "192.168.1.10",
        "::1",
    ]
    assert settings.content.html_js_heavy_domains == [
        "example.com",
        "spa.example.org",
    ]


@pytest.mark.unit
def test_dotenv_reaches_nested_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` reaches all nested sub-settings while respecting `Settings.model_config['env_file'] = None`."""
    for key in (
        "ENVIRONMENT",
        "APP_NAME",
        "DOWNLOADER_KEY",
        "REDIS_URI",
        "RATELIMIT_DEFAULT_LIMIT",
        "PDF_POOL_SIZE",
        "OCR_TIMEOUT_SECONDS",
        "OCR_MAX_PIXELS",
        "WEBHOOK_TIMEOUT_SECONDS",
        "WORKER_BROWSER_TASK_TIMEOUT",
        "CORS_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(key, raising=False)

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "\n".join(
            [
                "ENVIRONMENT=staging",
                "APP_NAME=Dotenv Downloader",
                "DOWNLOADER_KEY=dotenv-nested-key",
                "REDIS_URI=redis://dotenv-redis:6379/1",
                "RATELIMIT_DEFAULT_LIMIT=42/minute",
                "PDF_POOL_SIZE=5",
                "OCR_TIMEOUT_SECONDS=22.5",
                "OCR_MAX_PIXELS=12000000",
                "WEBHOOK_TIMEOUT_SECONDS=15.0",
                "WORKER_BROWSER_TASK_TIMEOUT=55.0",
                "CORS_ALLOWED_ORIGINS=https://dotenv.example.com,https://two.example.com",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Confirm test isolation: when Settings.model_config['env_file'] is None (as in conftest),
    # default Settings() ignores cwd .env
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    isolated = Settings()
    assert isolated.redis.redis_uri is None
    assert isolated.pdf.pool_size == 3

    # Explicit _env_file (and production env_file=".env") propagates to every nested section
    loaded = Settings(_env_file=str(dotenv_path))
    assert loaded.environment == "staging"
    assert loaded.app_name == "Dotenv Downloader"
    assert loaded.auth.api_key == "dotenv-nested-key"
    assert loaded.redis.redis_uri == "redis://dotenv-redis:6379/1"
    assert loaded.ratelimit.default_limit == "42/minute"
    assert loaded.pdf.pool_size == 5
    assert loaded.ocr.timeout == 22.5
    assert loaded.ocr.max_image_pixels == 12_000_000
    assert loaded.webhook.timeout == 15.0
    assert loaded.worker.remote_browser_timeout == 55.0
    assert loaded.cors.allowed_origins == [
        "https://dotenv.example.com",
        "https://two.example.com",
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ratelimit_and_ssrf_startup_validation_and_cloud_metadata_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`A-15`: Validate rate-limit strings at startup, wire `SSRF_BLOCK_CLOUD_METADATA`, and guard prod DNS."""
    from pydantic import ValidationError

    from downloader.config import RateLimitConfig, SSRFConfig
    from downloader.validation import SSRFProtectionError, validate_url_async

    # 1. Invalid rate-limit string fails immediately at model construction
    with pytest.raises(ValidationError, match="Invalid rate limit expression"):
        RateLimitConfig(default_limit="not-a-rate-limit")
    with pytest.raises(ValidationError, match="Invalid rate limit expression"):
        RateLimitConfig(download_limit="60/fortnights")

    # 2. SSRF_BLOCK_CLOUD_METADATA independently controls cloud metadata IPs and hostnames
    private_open_meta_blocked = Settings(
        ssrf=SSRFConfig(block_private_ips=False, block_cloud_metadata=True, resolve_dns=False)
    )
    with pytest.raises(SSRFProtectionError):
        await validate_url_async(
            "http://169.254.169.254/latest/meta-data/", private_open_meta_blocked
        )
    with pytest.raises(SSRFProtectionError):
        await validate_url_async(
            "http://metadata.google.internal/computeMetadata/v1/", private_open_meta_blocked
        )
    assert (
        await validate_url_async("http://10.0.0.5/internal", private_open_meta_blocked)
        == "http://10.0.0.5/internal"
    )

    both_open = Settings(
        ssrf=SSRFConfig(block_private_ips=False, block_cloud_metadata=False, resolve_dns=False)
    )
    assert (
        await validate_url_async("http://169.254.169.254/latest/meta-data/", both_open)
        == "http://169.254.169.254/latest/meta-data/"
    )
    assert (
        await validate_url_async("http://metadata.google.internal/computeMetadata/v1/", both_open)
        == "http://metadata.google.internal/computeMetadata/v1/"
    )

    # Disabling only SSRF_BLOCK_CLOUD_METADATA while SSRF_BLOCK_PRIVATE_IPS=true still blocks link-local/private metadata IPs
    meta_off_private_on = Settings(
        ssrf=SSRFConfig(block_private_ips=True, block_cloud_metadata=False, resolve_dns=False)
    )
    for meta_ip_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
    ):
        with pytest.raises(SSRFProtectionError):
            await validate_url_async(meta_ip_url, meta_off_private_on)

    # 3. Production refuses SSRF_RESOLVE_DNS=false unless SSRF_ALLOW_UNRESOLVED_DNS_PRODUCTION=true
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DOWNLOADER_KEY", "prod-secret-key")
    monkeypatch.setenv("SSRF_RESOLVE_DNS", "false")
    monkeypatch.delenv("SSRF_ALLOW_UNRESOLVED_DNS_PRODUCTION", raising=False)
    with pytest.raises(ValidationError, match="SSRF_RESOLVE_DNS=false"):
        Settings()

    monkeypatch.setenv("SSRF_ALLOW_UNRESOLVED_DNS_PRODUCTION", "true")
    opted_out = Settings()
    assert opted_out.ssrf.resolve_dns is False
    assert opted_out.ssrf.allow_unresolved_dns_production is True

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

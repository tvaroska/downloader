"""Documentation-contract tests mapping documented env vars to `Settings` fields (`R53`, `R75`)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import AliasChoices
from pydantic_settings import BaseSettings

from downloader.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_PREFIXES = (
    "ENVIRONMENT",
    "APP_NAME",
    "HTTP_",
    "PDF_",
    "BATCH_",
    "CONTENT_",
    "REDIS_",
    "DOWNLOADER_",
    "AUTH_",
    "LOG_",
    "SSRF_",
    "CORS_",
    "RATELIMIT_",
    "USER_AGENT",
    "RESPECT_ROBOTS_TXT",
    "ROBOTS_",
    "MAX_CRAWL_DELAY",
    "SCHEDULER_",
    "OCR_",
    "WEBHOOK_",
    "SHUTDOWN_",
    "DEPLOYMENT_MODE",
    "WORKER_",
    "OTEL_",
)

# Known spec-review item from R53: documented in spec/contracts.md and README.md
# without a corresponding runtime setting.
_KNOWN_SPEC_REVIEW_ENV_VARS = frozenset({"SHUTDOWN_POLL_INTERVAL"})

# Known .env.example drift: LoggingConfig uses env_prefix="LOG_" on log_level,
# log_rotation_size, and log_rotation_count (resolving to LOG_LOG_*).
_KNOWN_DOTENV_EXAMPLE_DRIFT = frozenset({"LOG_LEVEL", "LOG_ROTATION_COUNT", "LOG_ROTATION_SIZE"})


def _known_settings_env_vars() -> set[str]:
    known: set[str] = {"ENVIRONMENT", "APP_NAME"}
    for field in Settings.model_fields.values():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseSettings):
            prefix = ann.model_config.get("env_prefix", "")
            for fname, ffield in ann.model_fields.items():
                alias = ffield.validation_alias or ffield.alias
                if isinstance(alias, AliasChoices):
                    for choice in alias.choices:
                        known.add(str(choice))
                elif isinstance(alias, str):
                    known.add(alias)
                else:
                    known.add(f"{prefix}{fname.upper()}")
    return known


def _extract_env_vars(doc_path: Path) -> set[str]:
    tokens = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", doc_path.read_text(encoding="utf-8")))
    return {t for t in tokens if t.startswith(_ENV_PREFIXES)}


@pytest.mark.smoke
def test_spec_env_vars_resolve_to_settings_fields() -> None:
    """Every env var in `spec/contracts.md` resolves to a `Settings` field or alias (`R53`, `R75`)."""
    known = _known_settings_env_vars()
    spec_vars = _extract_env_vars(REPO_ROOT / "spec" / "contracts.md")
    missing = sorted(spec_vars - known - _KNOWN_SPEC_REVIEW_ENV_VARS)
    assert not missing, f"Spec env vars missing from Settings: {missing}"


@pytest.mark.smoke
@pytest.mark.parametrize(
    ("doc_relpath", "allowed_known_drift"),
    [
        ("README.md", _KNOWN_SPEC_REVIEW_ENV_VARS),
        ("AGENTS.md", frozenset()),
        (".env.example", _KNOWN_DOTENV_EXAMPLE_DRIFT),
    ],
)
def test_repo_docs_env_vars_resolve_to_settings_fields(
    doc_relpath: str, allowed_known_drift: frozenset[str]
) -> None:
    """Every env var in repo docs resolves to a `Settings` field or alias (`R75`)."""
    known = _known_settings_env_vars()
    doc_vars = _extract_env_vars(REPO_ROOT / doc_relpath)
    missing = sorted(doc_vars - known - allowed_known_drift)
    assert not missing, f"{doc_relpath} env vars missing from Settings: {missing}"

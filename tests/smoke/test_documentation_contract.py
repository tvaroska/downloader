"""Documentation-contract tests mapping documented env vars and defaults to `Settings` (`R53`, `R75`, `R102`, `R114`)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

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


def _settings_env_defaults() -> dict[str, Any]:
    """Build a mapping from every Settings env var name/alias to its default value."""
    cfg = Settings()
    defaults: dict[str, Any] = {
        "ENVIRONMENT": cfg.environment,
        "APP_NAME": cfg.app_name,
    }
    for field_name, field in Settings.model_fields.items():
        ann = field.annotation
        if isinstance(ann, type) and issubclass(ann, BaseSettings):
            sub_obj = getattr(cfg, field_name)
            prefix = ann.model_config.get("env_prefix", "")
            for fname, ffield in ann.model_fields.items():
                val = getattr(sub_obj, fname)
                alias = ffield.validation_alias or ffield.alias
                if isinstance(alias, AliasChoices):
                    for choice in alias.choices:
                        defaults[str(choice)] = val
                elif isinstance(alias, str):
                    defaults[alias] = val
                else:
                    defaults[f"{prefix}{fname.upper()}"] = val
    return defaults


def _known_settings_env_vars() -> set[str]:
    return set(_settings_env_defaults())


def _extract_env_vars(doc_path: Path) -> set[str]:
    tokens = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", doc_path.read_text(encoding="utf-8")))
    return {t for t in tokens if t.startswith(_ENV_PREFIXES)}


def _normalize_value_for_compare(raw_doc_val: str, actual: Any) -> tuple[str, str]:
    doc_clean = raw_doc_val.strip().strip('"').strip("'").strip("`")
    if actual is None:
        return doc_clean.lower(), "" if doc_clean == "" else "null"
    if isinstance(actual, bool):
        return doc_clean.lower(), "true" if actual else "false"
    if isinstance(actual, list):
        return doc_clean, ",".join(str(item) for item in actual)
    if isinstance(actual, float):
        try:
            return str(float(doc_clean)), str(float(actual))
        except ValueError:
            return doc_clean, str(actual)
    return doc_clean, str(actual)


@pytest.mark.smoke
def test_spec_env_vars_resolve_to_settings_fields() -> None:
    """Every env var in `spec/contracts.md` resolves to a `Settings` field or alias (`R53`, `R75`, `R114`)."""
    known = _known_settings_env_vars()
    spec_vars = _extract_env_vars(REPO_ROOT / "spec" / "contracts.md")
    missing = sorted(spec_vars - known)
    assert not missing, f"Spec env vars missing from Settings: {missing}"


@pytest.mark.smoke
@pytest.mark.parametrize("doc_relpath", ["README.md", "AGENTS.md", ".env.example"])
def test_repo_docs_env_vars_resolve_to_settings_fields(doc_relpath: str) -> None:
    """Every env var in repo docs resolves to a `Settings` field or alias with zero drift (`R75`, `R114`)."""
    known = _known_settings_env_vars()
    doc_vars = _extract_env_vars(REPO_ROOT / doc_relpath)
    missing = sorted(doc_vars - known)
    assert not missing, f"{doc_relpath} env vars missing from Settings: {missing}"


@pytest.mark.smoke
def test_documented_env_var_defaults_match_settings() -> None:
    """Every documented default in `.env.example` and `spec/contracts.md` matches `Settings()` (`R114`)."""
    defaults = _settings_env_defaults()
    mismatches: list[str] = []

    # 1. Verify uncommented KEY=VALUE assignments in .env.example.
    dotenv_lines = (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(dotenv_lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, raw_rhs = stripped.split("=", 1)
        key = key.strip()
        if key not in defaults:
            mismatches.append(f".env.example:{lineno}: unknown setting {key}")
            continue
        val_part = raw_rhs.split("#", 1)[0].strip()
        doc_norm, actual_norm = _normalize_value_for_compare(val_part, defaults[key])
        if doc_norm != actual_norm:
            mismatches.append(
                f".env.example:{lineno}: {key} documents {val_part!r}, Settings default is {defaults[key]!r}"
            )

    # 2. Verify markdown `| `ENV_VAR` | `default` |` table rows in spec/contracts.md.
    table_re = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]{2,})`\s*\|\s*`?([^`|]+?)`?\s*\|")
    spec_lines = (REPO_ROOT / "spec" / "contracts.md").read_text(encoding="utf-8").splitlines()
    for lineno, line in enumerate(spec_lines, start=1):
        match = table_re.match(line.strip())
        if not match:
            continue
        key, raw_default = match.group(1), match.group(2).strip()
        if key not in defaults:
            continue
        if raw_default.lower() in {"none", "null"} and defaults[key] is None:
            continue
        doc_norm, actual_norm = _normalize_value_for_compare(raw_default, defaults[key])
        if doc_norm != actual_norm:
            mismatches.append(
                f"spec/contracts.md:{lineno}: {key} documents {raw_default!r}, Settings default is {defaults[key]!r}"
            )

    assert not mismatches, "Documented default drift:\n" + "\n".join(mismatches)

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


@pytest.mark.smoke
def test_gate_ci_and_container_structural_guards() -> None:
    """Pin coverage floors, mypy, ast-grep, Trivy, non-root user, and shutdown budget (`T-8`)."""
    import importlib
    import json
    import tomllib

    yaml_mod = importlib.import_module("yaml")

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    cov_run = pyproject["tool"]["coverage"]["run"]
    cov_report = pyproject["tool"]["coverage"]["report"]
    assert cov_run.get("branch") is True
    pyproject_floor = float(cov_report["fail_under"])
    assert pyproject_floor >= 88.0

    # 1. scripts/gate.sh structural invariants
    gate_sh = (REPO_ROOT / "scripts" / "gate.sh").read_text(encoding="utf-8")
    assert ".venv/bin/ast-grep scan --rule specs/ast.rules.yaml" in gate_sh
    assert "command -v ast-grep" not in gate_sh
    assert re.search(r"mypy\b[^\n]*\bsrc/\s+tests/", gate_sh), (
        "scripts/gate.sh must run mypy on src/ tests/"
    )
    gate_cov_match = re.search(r"--cov-fail-under=([0-9]+(?:\.[0-9]+)?)", gate_sh)
    assert gate_cov_match is not None, "scripts/gate.sh must pass explicit --cov-fail-under"
    assert float(gate_cov_match.group(1)) >= pyproject_floor
    assert "--cov=src/downloader" in gate_sh
    assert "scripts/check_critical_coverage.py" in gate_sh
    assert " -n auto" in gate_sh

    justfile_text = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
    just_cov_match = re.search(r"--cov-fail-under=([0-9]+(?:\.[0-9]+)?)", justfile_text)
    assert just_cov_match is not None
    assert float(just_cov_match.group(1)) >= pyproject_floor
    assert "scripts/check_critical_coverage.py" in justfile_text

    # 2. cloudbuild.yaml structural invariants
    cb = yaml_mod.safe_load((REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    steps_by_id = {step["id"]: step for step in cb["steps"] if "id" in step}

    py_step_script = " ".join(steps_by_id["python-lint-typecheck-and-tests"]["args"])
    assert "ast-grep scan --rule specs/ast.rules.yaml" in py_step_script
    assert re.search(r"mypy\b[^\n]*\bsrc/\s+tests/", py_step_script)
    assert "--cov=src/downloader" in py_step_script
    assert "scripts/check_critical_coverage.py" in py_step_script
    cb_cov_match = re.search(r"--cov-fail-under=([0-9]+(?:\.[0-9]+)?)", py_step_script)
    assert cb_cov_match is not None
    assert float(cb_cov_match.group(1)) >= pyproject_floor

    trivy_step = steps_by_id["trivy-vulnerability-scan"]
    assert not trivy_step.get("allowFailure", False), "Trivy step must not allowFailure"
    trivy_script = " ".join(trivy_step["args"])
    assert "trivy:" in trivy_script and "image" in trivy_script
    assert "--pkg-types os,library" in trivy_script
    sev_match = re.search(r"--severity\s+([A-Z,]+)", trivy_script)
    assert sev_match is not None
    severities = set(sev_match.group(1).split(","))
    assert {"HIGH", "CRITICAL"} <= severities
    assert "--exit-code 1" in trivy_script

    push_wait_for = set(steps_by_id["push-release-image"]["waitFor"])
    assert {
        "trivy-vulnerability-scan",
        "verify-nonroot-and-healthcheck",
        "redis-integration-tests",
        "playwright-integration-tests",
        "loopback-e2e-tests",
    } <= push_wait_for

    # 3. Dockerfile non-root USER and uvicorn graceful shutdown budget
    dockerfile_lines = [
        ln.strip()
        for ln in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    user_directives = [
        ln.split(None, 1)[1].strip() for ln in dockerfile_lines if ln.upper().startswith("USER ")
    ]
    assert user_directives, "Dockerfile must declare a non-root USER"
    final_user = user_directives[-1].split(":", 1)[0]
    assert final_user not in {"root", "0"}, (
        f"Dockerfile final USER must be non-root, got {final_user!r}"
    )

    cmd_lines = [ln for ln in dockerfile_lines if ln.upper().startswith("CMD ")]
    assert cmd_lines, "Dockerfile must declare CMD"
    cmd_tokens = json.loads(cmd_lines[-1][4:].strip())
    assert "--timeout-graceful-shutdown" in cmd_tokens
    idx = cmd_tokens.index("--timeout-graceful-shutdown")
    graceful_budget = float(cmd_tokens[idx + 1])
    cfg = Settings()
    min_budget = cfg.shutdown.drain_timeout + cfg.shutdown.pre_stop_delay
    assert graceful_budget > min_budget, (
        f"Dockerfile --timeout-graceful-shutdown ({graceful_budget}s) <= {min_budget}s"
    )


@pytest.mark.smoke
def test_health_score_ratelimit_and_worker_metrics_contract_alignment() -> None:
    """`S-14`: Verify `spec/contracts.md` documents threshold-only health factors, per-replica memory limits, and worker `/metrics` auth."""
    from downloader.metrics import MetricsCollector

    collector = MetricsCollector()
    healthy = collector.get_system_health_score()
    assert healthy == {"overall_score": 100, "status": "healthy", "factors": {}}

    spec_text = (REPO_ROOT / "spec" / "contracts.md").read_text(encoding="utf-8")
    assert '"factors": {}' in spec_text
    assert "enforced per replica in process memory" in spec_text
    for line in spec_text.splitlines():
        if line.strip().startswith("| `WORKER_METRICS_PORT`"):
            assert "Bearer" in line and "X-API-Key" in line and "DOWNLOADER_KEY" in line
            break
    else:
        raise AssertionError("WORKER_METRICS_PORT row missing from spec/contracts.md")

    agents_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "SHUTDOWN_PRE_STOP_DELAY=2.0" in agents_text

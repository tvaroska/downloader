"""Unit tests for `scripts/check_critical_coverage.py` and `main.py` docs/health paths (`T-13`)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from downloader.config import Settings
from downloader.main import _probe_job_manager_status, app

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_coverage_checker():
    script_path = REPO_ROOT / "scripts" / "check_critical_coverage.py"
    spec = importlib.util.spec_from_file_location("check_critical_coverage", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.unit
def test_check_critical_coverage_resolves_critical_md_and_enforces_75_floor(
    tmp_path: Path,
) -> None:
    """`T-13`: Coverage checker resolves all `CRITICAL.md` Python modules and rejects `<75%` or `<88%`."""
    checker = _load_coverage_checker()
    modules = checker.resolve_critical_modules(REPO_ROOT)
    assert "src/downloader/auth.py" in modules
    assert "src/downloader/validation.py" in modules
    assert "src/downloader/crawling/robots.py" in modules
    assert "src/downloader/browser/manager.py" in modules

    passing_report = {
        "totals": {"percent_covered": 89.5},
        "files": {rel: {"summary": {"percent_covered": 82.0}} for rel in modules},
    }
    cov_ok = tmp_path / "cov_ok.json"
    cov_ok.write_text(json.dumps(passing_report), encoding="utf-8")
    assert checker.check_coverage_report(cov_ok, REPO_ROOT) == []

    low_module_report = {
        "totals": {"percent_covered": 89.5},
        "files": {
            rel: {"summary": {"percent_covered": 74.2 if rel.endswith("drain.py") else 85.0}}
            for rel in modules
        },
    }
    cov_low_mod = tmp_path / "cov_low_mod.json"
    cov_low_mod.write_text(json.dumps(low_module_report), encoding="utf-8")
    mod_violations = checker.check_coverage_report(cov_low_mod, REPO_ROOT)
    assert len(mod_violations) == 1
    assert "src/downloader/drain.py" in mod_violations[0]
    assert "75.00%" in mod_violations[0]

    low_total_report = {
        "totals": {"percent_covered": 87.4},
        "files": {rel: {"summary": {"percent_covered": 85.0}} for rel in modules},
    }
    cov_low_total = tmp_path / "cov_low_total.json"
    cov_low_total.write_text(json.dumps(low_total_report), encoding="utf-8")
    total_violations = checker.check_coverage_report(cov_low_total, REPO_ROOT)
    assert len(total_violations) == 1
    assert "88.00%" in total_violations[0]


@pytest.mark.unit
async def test_main_non_prod_docs_routes_and_health_job_manager_probe_cache() -> None:
    """Exercise `main.py` non-prod OpenAPI/Swagger/ReDoc routes and `/health` probe cache."""
    with TestClient(app) as client:
        app.state.settings = Settings(_env_file=None, environment="development")
        assert client.get("/openapi.json").status_code == 200
        assert client.get("/docs").status_code == 200
        assert client.get("/docs/oauth2-redirect").status_code == 200
        assert client.get("/redoc").status_code == 200

        app.state.settings = Settings(
            _env_file=None,
            environment="production",
            auth=Settings(_env_file=None).auth.model_copy(update={"api_key": "prod-key"}),
        )
        assert client.get("/openapi.json").status_code == 404
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404

    fake_jm = MagicMock()
    fake_jm.redis_client = MagicMock()
    fake_jm.redis_client.ping = AsyncMock(return_value=True)
    first = await _probe_job_manager_status(fake_jm)
    second = await _probe_job_manager_status(fake_jm)
    assert first["status"] == "connected"
    assert second["status"] == "connected"
    assert fake_jm.redis_client.ping.await_count == 1

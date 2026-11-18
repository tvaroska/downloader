"""Parametrised authentication matrix over every registered FastAPI route (`R15`)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from downloader.config import AuthConfig, Settings
from downloader.main import app

_PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)


def _collect_protected_route_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    schema = app.openapi()
    for path, methods_map in sorted(schema.get("paths", {}).items()):
        if path in _PUBLIC_PATHS:
            continue
        sample_path = (
            path.replace("{url}", "https://example.com")
            .replace("{url:path}", "https://example.com")
            .replace("{job_id}", "job-auth-check")
            .replace("{schedule_id}", "sched-auth-check")
        )
        for method in sorted(methods_map):
            method_upper = method.upper()
            if method_upper in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                cases.append((method_upper, sample_path))
    return cases


_PROTECTED_CASES = _collect_protected_route_cases()
assert len(_PROTECTED_CASES) >= 15, f"Expected >= 15 protected route cases, got {_PROTECTED_CASES}"


@pytest.mark.unit
@pytest.mark.parametrize(("method", "path"), _PROTECTED_CASES)
def test_every_route_except_health_requires_api_key(method: str, path: str) -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        prev_settings = getattr(app.state, "settings", None)
        app.state.settings = Settings(
            auth=AuthConfig(api_key="matrix-required-key")  # pragma: allowlist secret
        )
        try:
            body = {"url": "https://example.com"} if method == "POST" else None
            resp = client.request(method, path, json=body)
            assert resp.status_code == 401, (
                f"Expected 401 on {method} {path}, got {resp.status_code}"
            )
        finally:
            app.state.settings = prev_settings

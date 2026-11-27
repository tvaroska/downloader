"""Repository hygiene and Markdown link integrity smoke tests (`ADR-005`)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "logs",
    "node_modules",
}

_MONOREPO_PREFIX = "//depot/" + "google3"
_INTERNAL_CODENAMES = "|".join(
    [
        "OC" + "TO",
        "Fu" + "eki",
        "Ko" + "ku",
        "Sec" + "LM",
        "Si" + "an",
        "Code" + "Mind",
        "AQ" + "uA",
        "SF" + "AR",
        "OK" + "F",
        "Con" + "cord",
        "GS" + "U",
    ]
)
_CUSTOMER_PREFIXES = "|".join(
    [
        "~/customers",
        "~/notes",
        "sky-" + "did-",
        "prod-corp-" + "aitacg-",
        "bb-llm-" + "gateway-",
    ]
)

FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Hardcoded home directory path", re.compile(r"/usr/local/google/" + r"home/")),
    (
        "Local file URI scheme",
        re.compile(r"file:///(?!(?:etc/passwd|home/user/\.ssh/id_rsa|C:/Windows/))"),
    ),
    ("Internal Buganizer ID", re.compile(r"\bb" + r"/\d{6,}\b")),
    (
        "Internal shortlink",
        re.compile(
            r"(?<![a-zA-Z0-9_./-])go" + r"/(?!mod\b|sum\b|work\b|version\b)[a-zA-Z0-9_-]{3,}\b"
        ),
    ),
    ("Internal monorepo path", re.compile(rf"{_MONOREPO_PREFIX}|\bgoogle" + r"3\b")),
    ("Customer or personal vault workspace path", re.compile(_CUSTOMER_PREFIXES)),
    ("Internal codename", re.compile(rf"\b({_INTERNAL_CODENAMES})\b")),
]

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")


def _iter_repo_files(*, suffix: str | None = None) -> list[Path]:
    files: list[Path] = []
    for root, dirs, filenames in os.walk(REPO_ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        root_path = Path(root)
        for fname in sorted(filenames):
            if suffix is not None and not fname.endswith(suffix):
                continue
            path = root_path / fname
            rel_parts = path.relative_to(REPO_ROOT).parts
            if rel_parts in {
                ("uv.lock",),
                (".coverage",),
                ("tests", "smoke", "test_repo_hygiene.py"),
            }:
                continue
            files.append(path)
    return files


@pytest.mark.smoke
def test_zero_github_workflows() -> None:
    """Enforce Cloud Build-only CI/CD (zero `.github/workflows/` files)."""
    workflows_dir = REPO_ROOT / (".git" + "hub") / "workflows"
    workflow_files = list(workflows_dir.rglob("*")) if workflows_dir.exists() else []
    assert not workflow_files, f"Forbidden CI workflow files found: {workflow_files}"


@pytest.mark.smoke
def test_zero_adr005_internal_or_machine_path_leaks() -> None:
    """Ensure repository contains zero customer, internal codename, or local machine path leaks."""
    violations: list[str] = []
    for fpath in _iter_repo_files():
        try:
            text = fpath.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = fpath.relative_to(REPO_ROOT)
        for label, pattern in FORBIDDEN_PATTERNS:
            match = pattern.search(text)
            if match:
                violations.append(f"{rel}: {label} ({match.group(0)!r})")
    assert not violations, "Repository hygiene violations found:\n" + "\n".join(violations)


@pytest.mark.smoke
def test_all_relative_markdown_links_resolve() -> None:
    """Verify all repo-relative Markdown links resolve to existing in-repo files."""
    broken: list[str] = []
    for md_file in _iter_repo_files(suffix=".md"):
        text = md_file.read_text(encoding="utf-8")
        stripped = _INLINE_CODE_RE.sub("", _FENCED_CODE_RE.sub("", text))
        for match in _MD_LINK_RE.finditer(stripped):
            raw_target = match.group(1).split("#", 1)[0]
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:")):
                continue
            target = (md_file.parent / raw_target.replace("%20", " ")).resolve()
            if not target.exists() or not target.is_relative_to(REPO_ROOT):
                broken.append(f"{md_file.relative_to(REPO_ROOT)}: {raw_target}")
    assert not broken, "Broken relative Markdown links found:\n" + "\n".join(broken)


@pytest.mark.smoke
def test_uv_lock_uses_public_pypi_only() -> None:
    """Ensure uv.lock only pins public PyPI registries so Cloud Build can fetch wheels."""
    lock_file = REPO_ROOT / "uv.lock"
    assert lock_file.exists(), "uv.lock must exist"
    text = lock_file.read_text(encoding="utf-8")
    registries = set(re.findall(r'registry\s*=\s*"([^"]+)"', text))
    assert registries == {"https://pypi.org/simple"}, (
        f"Non-public registry URLs found in uv.lock (run `uv lock --no-config --default-index https://pypi.org/simple`): {registries}"
    )


@pytest.mark.smoke
def test_hygiene_patterns_positive_control(tmp_path: Path) -> None:
    """Positive control verifying every `FORBIDDEN_PATTERNS` regex catches synthetic leaks (`R56`)."""
    synthetic_probes: dict[str, str] = {
        "Hardcoded home directory path": "/usr/local/" + "google/home/alice/project",
        "Local file URI scheme": "file://" + "/workspace/notes/secret.md",
        "Internal Buganizer ID": "See " + "b" + "/123456789 for details",
        "Internal shortlink": "Visit " + "go" + "/internal-design-doc",
        "Internal monorepo path": "//depot/" + "google3/pkg/BUILD",
        "Customer or personal vault workspace path": "~/cust" + "omers/acme/notes.md",
        "Internal codename": "Powered by " + "Con" + "cord telemetry",
    }
    assert set(synthetic_probes) == {label for label, _ in FORBIDDEN_PATTERNS}

    for label, pattern in FORBIDDEN_PATTERNS:
        probe_file = tmp_path / f"{label.lower().replace(' ', '_')}.txt"
        probe_file.write_text(synthetic_probes[label], encoding="utf-8")
        content = probe_file.read_text(encoding="utf-8")
        assert pattern.search(content) is not None, f"Pattern {label!r} missed synthetic leak"

    clean_file = tmp_path / "clean.md"
    clean_file.write_text(
        "Standard public doc referencing file:///etc/passwd, go/mod, and https://pypi.org/simple.\n",
        encoding="utf-8",
    )
    clean_text = clean_file.read_text(encoding="utf-8")
    for label, pattern in FORBIDDEN_PATTERNS:
        assert pattern.search(clean_text) is None, f"Pattern {label!r} false-positived on clean doc"


@pytest.mark.smoke
def test_governance_spine_and_claude_symlink() -> None:
    """Enforce governance file size ceilings, `CLAUDE.md -> AGENTS.md` symlink, and AST rules (`R56`)."""
    agents_md = REPO_ROOT / "AGENTS.md"
    claude_md = REPO_ROOT / "CLAUDE.md"
    critical_md = REPO_ROOT / "CRITICAL.md"
    decisions_md = REPO_ROOT / "DECISIONS.md"
    ast_rules = REPO_ROOT / "specs" / "ast.rules.yaml"

    assert agents_md.is_file(), "AGENTS.md must exist"
    assert agents_md.stat().st_size <= 24_000, "AGENTS.md must stay <= 24,000 bytes"

    assert claude_md.is_symlink(), "CLAUDE.md must be a symlink to AGENTS.md"
    assert os.readlink(claude_md) == "AGENTS.md", (
        "CLAUDE.md must be a relative symlink to AGENTS.md"
    )

    assert critical_md.is_file(), "CRITICAL.md must exist"
    assert critical_md.stat().st_size <= 5_120, "CRITICAL.md must stay <= 5 KB"

    assert decisions_md.is_file(), "DECISIONS.md must exist"
    assert decisions_md.stat().st_size <= 25_600, "DECISIONS.md must stay <= 25 KB"

    assert ast_rules.is_file() and ast_rules.stat().st_size > 0, "specs/ast.rules.yaml must exist"


@pytest.mark.smoke
def test_version_strings_match_package_manifest() -> None:
    """Active package, SDK, and contract manifests must agree on the current service version (`R111`)."""
    root_pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = str(root_pyproject["project"]["version"])

    from downloader import __version__

    assert __version__ == expected

    py_sdk = tomllib.loads(
        (REPO_ROOT / "sdks" / "python" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert str(py_sdk["project"]["version"]) == expected

    js_pkg = json.loads(
        (REPO_ROOT / "sdks" / "javascript" / "package.json").read_text(encoding="utf-8")
    )
    assert str(js_pkg["version"]) == expected

    spec_text = (REPO_ROOT / "spec" / "contracts.md").read_text(encoding="utf-8")
    assert f"(service `{expected}`)" in spec_text
    assert f'"version": "{expected}"' in spec_text


@pytest.mark.smoke
def test_dockerfile_and_cloudbuild_image_hardening() -> None:
    """`S-15`: Pin digests on base/CI images, drop `curl`, and keep `/app` root-owned read-only."""
    import importlib

    yaml_mod = importlib.import_module("yaml")
    dockerfile_text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("FROM "):
            image_ref = stripped.split()[1]
            assert "@sha256:" in image_ref, f"Dockerfile FROM must pin digest: {stripped}"

    non_comment_lines = "\n".join(
        ln for ln in dockerfile_text.splitlines() if not ln.strip().startswith("#")
    )
    assert "curl" not in non_comment_lines, "Dockerfile must not install or invoke curl"
    assert "chown -R root:root /app" in dockerfile_text
    assert "chown -R appuser:appuser /app" not in dockerfile_text

    cb = yaml_mod.safe_load((REPO_ROOT / "cloudbuild.yaml").read_text(encoding="utf-8"))
    for step in cb["steps"]:
        step_name = step["name"]
        if step_name.startswith(("gcr.io/", "${_REGION}-docker.pkg.dev/")):
            continue
        assert "@sha256:" in step_name, (
            f"Cloud Build step {step.get('id')} must pin digest: {step_name}"
        )

    trivy_script = " ".join(
        next(s["args"] for s in cb["steps"] if s.get("id") == "trivy-vulnerability-scan")
    )
    assert "aquasec/trivy:" in trivy_script and "@sha256:" in trivy_script
    redis_script = " ".join(
        next(s["args"] for s in cb["steps"] if s.get("id") == "redis-integration-tests")
    )
    assert "redis:7-alpine@sha256:" in redis_script


@pytest.mark.smoke
def test_test_files_stay_under_800_lines_and_subsystem_named() -> None:
    """`T-14` (`R108`): Every test file stays `<= 800` lines and avoids `test_v0*` version names."""
    tests_root = REPO_ROOT / "tests"
    oversized: list[str] = []
    version_named: list[str] = []
    for py_file in sorted(tests_root.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        line_count = len(py_file.read_text(encoding="utf-8").splitlines())
        if line_count > 800:
            oversized.append(f"{rel} ({line_count} lines > 800)")
        if re.match(r"^test_v\d+", py_file.name):
            version_named.append(rel)
    assert not oversized, "Oversized test files (>800 lines):\n" + "\n".join(oversized)
    assert not version_named, "Version-named test files found:\n" + "\n".join(version_named)

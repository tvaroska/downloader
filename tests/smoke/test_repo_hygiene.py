"""Repository hygiene and Markdown link integrity smoke tests (`ADR-005`)."""

from __future__ import annotations

import os
import re
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
    "htmlcov",
    "logs",
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

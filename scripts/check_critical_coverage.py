#!/usr/bin/env python3
"""Verify overall (`>= 88%`) and per-module (`>= 75%`) branch coverage for `CRITICAL.md` paths."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

OVERALL_FLOOR = 88.0
PER_MODULE_FLOOR = 75.0

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")


def _expand_critical_token(repo_root: Path, token: str) -> list[str]:
    if not token.startswith("src/downloader/"):
        return []
    if token.endswith(("/*", "/")):
        suffix_len = 2 if token.endswith("/*") else 1
        folder = repo_root / token[:-suffix_len]
        if not folder.is_dir():
            return []
        return [
            py.relative_to(repo_root).as_posix()
            for py in folder.rglob("*.py")
            if py.name != "__init__.py"
        ]
    if token.endswith(".py"):
        target = repo_root / token
        if target.is_file() and target.name != "__init__.py":
            return [target.relative_to(repo_root).as_posix()]
    return []


def resolve_critical_modules(repo_root: Path, critical_md: Path | None = None) -> list[str]:
    """Return sorted repo-relative `src/downloader/.../*.py` paths listed in `CRITICAL.md`."""
    doc_path = critical_md or (repo_root / "CRITICAL.md")
    matched: set[str] = set()
    for line in doc_path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("|"):
            continue
        first_col = line.split("|", 2)[1]
        for raw in _CODE_SPAN_RE.findall(first_col):
            matched.update(_expand_critical_token(repo_root, raw.strip()))
    return sorted(matched)


def check_coverage_report(
    coverage_json_path: Path,
    repo_root: Path,
    *,
    overall_floor: float = OVERALL_FLOOR,
    per_module_floor: float = PER_MODULE_FLOOR,
    critical_md: Path | None = None,
) -> list[str]:
    """Return a list of human-readable coverage violations (empty when passing)."""
    data = json.loads(coverage_json_path.read_text(encoding="utf-8"))
    violations: list[str] = []

    total_pct = float(data.get("totals", {}).get("percent_covered", 0.0))
    if total_pct < overall_floor:
        violations.append(f"Overall coverage {total_pct:.2f}% is below floor {overall_floor:.2f}%")

    raw_files: dict[str, dict[str, object]] = data.get("files", {})
    repo_prefix = repo_root.as_posix() + "/"
    normalized_files = {
        (
            Path(k).as_posix()[len(repo_prefix) :]
            if Path(k).as_posix().startswith(repo_prefix)
            else Path(k).as_posix()
        ): v
        for k, v in raw_files.items()
    }

    critical_modules = resolve_critical_modules(repo_root, critical_md=critical_md)
    if not critical_modules:
        return ["No critical Python modules resolved from CRITICAL.md"]

    for rel_mod in critical_modules:
        entry = normalized_files.get(rel_mod)
        if entry is None:
            violations.append(f"{rel_mod}: missing from coverage JSON report")
            continue
        summary = entry.get("summary")
        pct = float(summary.get("percent_covered", 0.0)) if isinstance(summary, dict) else 0.0
        if pct < per_module_floor:
            violations.append(
                f"{rel_mod}: branch coverage {pct:.2f}% is below CRITICAL.md floor {per_module_floor:.2f}%"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("Usage: check_critical_coverage.py <coverage.json>", file=sys.stderr)
        return 2
    repo_root = Path(__file__).resolve().parents[1]
    violations = check_coverage_report(Path(args[0]), repo_root)
    if violations:
        for item in violations:
            print(f"COVERAGE VIOLATION: {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

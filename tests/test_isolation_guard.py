"""Architectural port-isolation and declarative AST rule verification (`R45`, `R46`, `R54`, `R72`)."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PKG = REPO_ROOT / "src" / "downloader"
AST_RULES_PATH = REPO_ROOT / "specs" / "ast.rules.yaml"

_DRIVING_ADAPTER_PATHS = frozenset(
    {
        "main.py",
        "api.py",
        "worker.py",
    }
)


def _find_ast_grep() -> Path:
    venv_bin = Path(sys.executable).parent / "ast-grep"
    if venv_bin.is_file():
        return venv_bin
    found = shutil.which("ast-grep")
    assert found is not None, "ast-grep binary is required for AST rule verification"
    return Path(found)


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts and not p.name.startswith(".")
    )


def _extract_imports(source: str, rel_pkg_path: str) -> list[str]:
    pkg_parts = ["downloader", *Path(rel_pkg_path).parent.parts]
    if pkg_parts[-1] == ".":
        pkg_parts.pop()
    imports: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                up = node.level - 1
                anchor_parts = pkg_parts[: max(0, len(pkg_parts) - up)]
                anchor = ".".join(anchor_parts)
            else:
                anchor = ""
            base = ".".join(p for p in (anchor, node.module or "") if p)
            imports.append(base)
            imports.extend(f"{base}.{alias.name}" for alias in node.names)
    return imports


def _check_pkg_file(rel: str, text: str, dual_prefix: str) -> list[str]:
    errors: list[str] = []
    targets = _extract_imports(text, rel)
    for target in targets:
        if target == dual_prefix or target.startswith(f"{dual_prefix}."):
            errors.append(f"src/downloader/{rel}: dual-module import {target}")

    if rel.startswith("services/"):
        for target in targets:
            if target.split(".", 1)[0] in {"fastapi", "starlette"}:
                errors.append(f"src/downloader/{rel}: forbidden web framework import {target}")

    is_driving = rel in _DRIVING_ADAPTER_PATHS or rel.startswith("routes/")
    if not is_driving:
        for target in targets:
            if target in {"pytest", "downloader.main", "downloader.routes"} or target.startswith(
                ("pytest.", "downloader.main.", "downloader.routes.")
            ):
                errors.append(f"src/downloader/{rel}: forbidden upward/test import {target}")
        if '"pytest" in sys.modules' in text or "'pytest' in sys.modules" in text:
            errors.append(f"src/downloader/{rel}: forbidden pytest branch in production code")
    return errors


@pytest.mark.smoke
def test_services_and_core_respect_hexagonal_boundaries() -> None:
    """Verify `services/` never imports FastAPI/Starlette and core never imports `main`, `routes`, or `pytest`."""
    violations: list[str] = []
    dual_prefix = "src." + "downloader"

    for py_file in _iter_python_files(SRC_PKG):
        rel = py_file.relative_to(SRC_PKG).as_posix()
        violations.extend(_check_pkg_file(rel, py_file.read_text(encoding="utf-8"), dual_prefix))

    assert not violations, "Hexagonal boundary violations:\n" + "\n".join(violations)


@pytest.mark.smoke
def test_zero_dual_module_imports_across_tests_and_examples() -> None:
    """Verify `tests/` and `examples/` import `downloader.*` rather than `src.downloader.*` (`R72`)."""
    dual_prefix = "src." + "downloader"
    bad: list[str] = []
    for folder in (REPO_ROOT / "tests", REPO_ROOT / "examples"):
        for py_file in _iter_python_files(folder):
            if py_file.resolve() == Path(__file__).resolve():
                continue
            source = py_file.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == dual_prefix or alias.name.startswith(f"{dual_prefix}."):
                            bad.append(f"{py_file.relative_to(REPO_ROOT)}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if (
                        node.module == dual_prefix
                        or node.module.startswith(f"{dual_prefix}.")
                        or (
                            node.module == "src" and any(a.name == "downloader" for a in node.names)
                        )
                    ):
                        bad.append(f"{py_file.relative_to(REPO_ROOT)}: from {node.module}")
    assert not bad, "Dual-module imports found:\n" + "\n".join(bad)


@pytest.mark.smoke
def test_ast_grep_repo_scan_passes() -> None:
    """Run `ast-grep scan --rule specs/ast.rules.yaml` against the live repository."""
    ast_grep = _find_ast_grep()
    res = subprocess.run(
        [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, f"ast-grep failed on repo:\n{res.stdout}\n{res.stderr}"


@pytest.mark.smoke
def test_ast_rules_positive_and_negative_controls(tmp_path: Path) -> None:
    """Verify every rule in `specs/ast.rules.yaml` catches all `invalid_examples` and passes `valid_examples`."""
    ast_grep = _find_ast_grep()
    docs: list[dict[str, Any]] = [
        doc for doc in yaml.safe_load_all(AST_RULES_PATH.read_text(encoding="utf-8")) if doc
    ]
    assert len(docs) >= 5

    for doc in docs:
        rule_id: str = doc["id"]
        meta: dict[str, Any] = doc.get("metadata", {})
        rel_test_path: str = meta["test_path"]
        invalid_examples: list[str] = meta.get("invalid_examples", [])
        valid_examples: list[str] = meta.get("valid_examples", [])
        assert invalid_examples, f"Rule {rule_id} must declare invalid_examples"
        assert valid_examples, f"Rule {rule_id} must declare valid_examples"

        for snippet in invalid_examples:
            probe_root = tmp_path / "invalid"
            if probe_root.exists():
                shutil.rmtree(probe_root)
            probe_file = probe_root / rel_test_path
            probe_file.parent.mkdir(parents=True, exist_ok=True)
            probe_file.write_text(
                f"def _probe():\n    {snippet.replace(chr(10), chr(10) + '    ')}\n",
                encoding="utf-8",
            )

            res = subprocess.run(
                [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
                cwd=probe_root,
                capture_output=True,
                text=True,
                check=False,
            )
            combined = f"{res.stdout}\n{res.stderr}"
            assert res.returncode != 0 and rule_id in combined, (
                f"Rule {rule_id} missed invalid example {snippet!r} at {rel_test_path}:\n{combined}"
            )

        for snippet in valid_examples:
            probe_root = tmp_path / "valid"
            if probe_root.exists():
                shutil.rmtree(probe_root)
            probe_file = probe_root / rel_test_path
            probe_file.parent.mkdir(parents=True, exist_ok=True)
            probe_file.write_text(
                f"def _probe():\n    {snippet.replace(chr(10), chr(10) + '    ')}\n",
                encoding="utf-8",
            )

            res = subprocess.run(
                [str(ast_grep), "scan", "--rule", str(AST_RULES_PATH), "."],
                cwd=probe_root,
                capture_output=True,
                text=True,
                check=False,
            )
            assert res.returncode == 0, (
                f"Rule {rule_id} false-positived on valid example {snippet!r} at {rel_test_path}:\n"
                f"{res.stdout}\n{res.stderr}"
            )

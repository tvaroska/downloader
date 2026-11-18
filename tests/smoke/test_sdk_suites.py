"""Smoke gate running the Python, JavaScript/TypeScript, and Go SDK suites (`R26`)."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.mark.smoke
def test_python_sdk_suite_passes() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            str(REPO_ROOT / "sdks/python/tests"),
            "-c",
            str(REPO_ROOT / "sdks/python/pyproject.toml"),
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"Python SDK tests failed:\n{proc.stdout}\n{proc.stderr}"


@pytest.mark.smoke
def test_javascript_sdk_suite_passes_without_untracked_build() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed in PATH")

    js_dir = REPO_ROOT / "sdks/javascript"
    dist_dir = js_dir / "dist"
    assert not dist_dir.exists(), "sdks/javascript must not rely on an untracked dist/ directory"

    proc = subprocess.run(
        [node, "--test", "test/client.test.mjs"],
        cwd=js_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"JS SDK tests failed:\n{proc.stdout}\n{proc.stderr}"

    tsc = shutil.which("tsc")
    if tsc is not None:
        tc_proc = subprocess.run(
            [tsc, "--noEmit", "-p", "tsconfig.json"],
            cwd=js_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        assert tc_proc.returncode == 0, f"JS SDK tsc failed:\n{tc_proc.stdout}\n{tc_proc.stderr}"


@pytest.mark.smoke
@pytest.mark.timeout(90)
def test_go_sdk_gofmt_vet_and_race_suite_pass() -> None:
    go_bin = shutil.which("go")
    gofmt_bin = shutil.which("gofmt")
    if go_bin is None or gofmt_bin is None:
        pytest.skip("go toolchain not installed in PATH")

    go_dir = REPO_ROOT / "sdks/go"
    fmt_proc = subprocess.run(
        [gofmt_bin, "-s", "-l", "."],
        cwd=go_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    assert fmt_proc.returncode == 0 and not fmt_proc.stdout.strip(), (
        f"Unformatted Go SDK files:\n{fmt_proc.stdout}"
    )

    gocache = pathlib.Path(tempfile.gettempdir()) / "go-build-downloader-sdk"
    gocache.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GOCACHE": str(gocache)}
    vet_proc = subprocess.run(
        [go_bin, "vet", "./..."],
        cwd=go_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert vet_proc.returncode == 0, f"go vet failed:\n{vet_proc.stdout}\n{vet_proc.stderr}"

    test_proc = subprocess.run(
        [go_bin, "test", "-race", "./..."],
        cwd=go_dir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert test_proc.returncode == 0, (
        f"go test -race failed:\n{test_proc.stdout}\n{test_proc.stderr}"
    )

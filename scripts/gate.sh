#!/usr/bin/env bash
# Canonical verification gate for downloader.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

export PATH="${HOME}/.local/bin:${PATH}"
export PYTHONDONTWRITEBYTECODE=1

RUN_TMP="$(mktemp -d /tmp/downloader_gate.XXXXXX)"
export COVERAGE_FILE="${RUN_TMP}/.coverage"
trap 'rm -rf "${RUN_TMP}"' EXIT

if [[ -f "specs/ast.rules.yaml" ]]; then
  .venv/bin/ast-grep scan --rule specs/ast.rules.yaml .
fi

if [[ $# -eq 0 ]]; then
  uv lock --check --offline --no-config
  .venv/bin/ruff check --no-cache src/ tests/ examples/
  .venv/bin/ruff format --check --no-cache src/ tests/ examples/
  .venv/bin/mypy --cache-dir=/tmp/top3-mypy-downloader src/ tests/
  .venv/bin/pytest -p no:cacheprovider --cov=src/downloader --cov-fail-under=83 -n auto -rs -m "not (integration or e2e or network or requires_redis or requires_playwright)"
else
  .venv/bin/pytest -p no:cacheprovider "$@"
fi

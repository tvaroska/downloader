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

if [[ -f "specs/ast.rules.yaml" ]] && command -v ast-grep >/dev/null 2>&1; then
  ast-grep scan --rule specs/ast.rules.yaml
fi

if [[ $# -eq 0 ]]; then
  uv lock --check --offline --no-config
  uv run --no-sync ruff check --no-cache src/ tests/ examples/
  uv run --no-sync ruff format --check --no-cache src/ tests/ examples/
  uv run --no-sync mypy --cache-dir=/tmp/top3-mypy-downloader src/ tests/
  uv run --no-sync pytest -p no:cacheprovider --cov=src/downloader -m "not (integration or e2e or network or requires_redis or requires_playwright)"
else
  uv run --no-sync pytest -p no:cacheprovider "$@"
fi

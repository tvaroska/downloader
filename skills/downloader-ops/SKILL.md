---
name: downloader-ops
description: REST API Downloader operational runbook covering single- vs multi-format Accept content negotiation, fast offline uv verification, Docker + Redis + Playwright integration/E2E testing, Cloud Build + Flux CD releases, CRITICAL.md escalation, and detect-secrets baseline maintenance. Use whenever modifying routes, browser rendering, CI/CD, releases, or security boundaries in this repository.
---

# Downloader Operations & Development Guardrails

## 1. Single- vs Multi-Format `Accept` Wire Contract (`GET /{url}`)

[`src/downloader/routes/download.py`](../../src/downloader/routes/download.py) and [`src/downloader/services/content_processor.py`](../../src/downloader/services/content_processor.py) implement two distinct response shapes depending on how many MIME types the client sends in `Accept`:

1. **Single Format (`0` or `1` requested MIME type — default `text/plain`):**
   - Returns the raw payload directly with matching `Content-Type`:
     - `Accept: text/plain` (or omitted) -> plain extracted article text (`text/plain`)
     - `Accept: text/markdown` -> Markdown string (`text/markdown`)
     - `Accept: text/html` -> raw or Playwright-rendered HTML (`text/html`)
     - `Accept: application/pdf` -> raw PDF binary bytes (`application/pdf`)
     - `Accept: application/json` -> JSON envelope with base64 content and metadata (`application/json`)
   - **Downstream caller invariant:** Clients requesting a single non-JSON format (`Accept: text/html`, `text/markdown`, `text/plain`) **must** read `response.text` / `response.content` — calling `response.json()` will fail with `JSONDecodeError`.
2. **Multi-Format (`2+` MIME types, e.g. `Accept: text/html, text/markdown`):**
   - Returns `application/json` keyed by MIME type (`{"text/html": "...", "text/markdown": "...", "application/pdf": "<base64>", "errors": {...}}`).
3. **Browser Rendering Query Parameters:**
   - `?render=true` forces headless Chromium rendering via Playwright, bypassing static HTML auto-detection.
   - `?wait_for=<css-selector>` implies `render=true` and waits up to 10 seconds for the selector before capturing DOM output.

---

## 2. Fast Offline Verification (`uv run --no-sync`)

Run all checks from the repository root against the locked `.venv`:

```bash
uv sync --frozen
uv run --no-sync ruff check src/ tests/ examples/
uv run --no-sync ruff format --check src/ tests/ examples/
uv run --no-sync mypy src/
uv run --no-sync pytest -m "not (integration or e2e or network or requires_redis or requires_playwright)"
```

- `tests/conftest.py` provides deterministic offline DNS resolution for fixture domains (`example.com`, `test.com`, `success.com`, `fail.com`, `google.com`, `localhost` -> `127.0.0.1`, unknown -> `socket.gaierror`) so SSRF loopback and DNS-failure unit tests stay real without live network calls.
- `PlaywrightPDFGenerator` initializes its Chromium `BrowserPool` lazily on first `generate_pdf()` or `render_html_with_playwright()` call, keeping mocked `TestClient(app)` tests sub-second with zero Chromium subprocesses.
- Use `just fmt` to apply both `ruff check --fix` and `ruff format` across `src/`, `tests/`, and `examples/`.

---

## 3. Docker, Redis & Playwright Integration / E2E Testing

Stateless mode (`REDIS_URI` unset) keeps `GET /{url}`, `/health`, and `/metrics*` active while disabling `JobManager` and `SchedulerService` (returning HTTP `503` on `/batch`, `/jobs/*`, and `/schedules*`). To test stateful and browser tiers locally:

```bash
# 1. Install Chromium for Playwright integration tests
uv run --no-sync playwright install chromium

# 2. Start local Redis for batch & schedule suites
docker run -d --rm --name downloader-redis -p 6379:6379 redis:7-alpine

# 3. Run component & browser integration tests
REDIS_URI=redis://localhost:6379 uv run --no-sync pytest -m integration -v
uv run --no-sync pytest -m requires_playwright -v

# 4. Run full containerized E2E suite (Docker Compose + Redis + examples/*.py)
just e2e
# Inspect logs/e2e-*.log if any example script fails; tear down Redis when finished:
docker stop downloader-redis
```

---

## 4. Cloud Build & GKE Autopilot / Flux CD Release Flow

CI/CD is defined exclusively in [`cloudbuild.yaml`](../../cloudbuild.yaml) and [`ci/trigger.yaml`](../../ci/trigger.yaml):

- **`downloader-main` (push to `main`):** Runs `uv sync --frozen`, `ruff check`, `ruff format --check`, `mypy src/`, and fast `pytest`, then builds [`Dockerfile`](../../Dockerfile) (`python:3.13-slim` + `uv sync --frozen --no-dev --no-editable` + Playwright Chromium) and verifies `python -c "import src.downloader.main"` without pushing release tags.
- **`downloader-tag` (annotated tag `vX.Y.Z`):** Runs the full verification gate and pushes `us-central1-docker.pkg.dev/sites-470716/containers/downloader:${TAG_NAME}` and `:latest` for Flux CD GitOps rollout to `https://download.tvaroska.sk`.
- **Lockfile Release Rule:** Whenever bumping `version` in [`pyproject.toml`](../../pyproject.toml), always run `uv lock --no-config --default-index https://pypi.org/simple` and commit the updated `uv.lock` before tagging `vX.Y.Z`; otherwise `uv sync --frozen` in `cloudbuild.yaml` and `Dockerfile` will fail the tag build.

---

## 5. `CRITICAL.md` Escalation & `.secrets.baseline` Hygiene

1. **Protected Paths ([`CRITICAL.md`](../../CRITICAL.md)):** Any change touching `spec/`, `src/downloader/auth.py`, `src/downloader/validation.py`, `src/downloader/config.py`, `src/downloader/quota/*`, `src/downloader/ratelimit_middleware.py`, `src/downloader/browser/`, `Dockerfile`, `docker-compose.prod.yml`, `cloudbuild.yaml`, or `ci/trigger.yaml` requires explicit security and contract review before commit. `spec/` is status-free and protected — propose contract updates for review rather than editing `spec/` unprompted.
2. **Secrets Baseline ([`.secrets.baseline`](../../.secrets.baseline)):** Pre-commit runs `detect-secrets`. When tracked files with allowlisted false-positive placeholders move or change line numbers, regenerate cleanly rather than hand-editing JSON hashes or line numbers:
   ```bash
   uv run --no-sync detect-secrets scan --update .secrets.baseline
   ```

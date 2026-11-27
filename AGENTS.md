# Downloader — Agent Instructions (`AGENTS.md`)

Canonical operating brief for all coding agents working in this repository (`CLAUDE.md` is a relative symlink to `AGENTS.md`).

## Project Overview

REST API Downloader — FastAPI service for programmatic URL downloading, article extraction, stateless-first ethical crawling (`robots.txt` + `Crawl-delay`), headless Chromium rendering, OCR, and optional Redis-backed batch/scheduled jobs.

- **Version:** 0.8.4
- **Stack:** Python 3.13+, FastAPI, httpx, Redis, Playwright, pytesseract/Pillow, OpenTelemetry (opt-in)
- **Registry:** `us-central1-docker.pkg.dev/sites-470716/containers/downloader` ([`cloudbuild.yaml`](cloudbuild.yaml) + GKE Autopilot / Flux CD GitOps)
- **Production domain:** `https://download.tvaroska.sk`

## Development Commands

```bash
uv sync --frozen                                  # Hydrate locked environment
just dev                                          # Uvicorn hot-reload on :8000
just worker                                       # Standalone split worker (DEPLOYMENT_MODE=worker)
just split                                        # Redis + API + worker via Docker Compose

# Lint, format, AST rules, type-check
just fmt                                          # ruff check --fix + ruff format
uv run --no-sync ruff check src/ tests/ examples/
uv run --no-sync ruff format --check src/ tests/ examples/
ast-grep scan --rule specs/ast.rules.yaml .
uv run --no-sync mypy src/ tests/

# Fast offline gate (>=88% branch coverage + >=75% per CRITICAL.md module; no Redis/Chromium)
uv run --no-sync pytest --cov=src/downloader -m "not (integration or e2e or network or requires_redis or requires_playwright)"
uv run --no-sync pytest sdks/python/tests -c sdks/python/pyproject.toml -v
node --test sdks/javascript/test/client.test.mjs
go -C sdks/go vet ./... && go -C sdks/go test -v ./...

# Targeted tiers & Docker
uv run --no-sync pytest -m smoke -v               # Hygiene, auth & contracts (<15s, ~106 tests)
uv run --no-sync pytest -m integration -v         # Real Playwright / Redis tests
uv run --no-sync pytest -m e2e -v                 # Full Docker Compose + Redis E2E
just docker                                       # Build + push image to Artifact Registry
```

## Architecture

```
src/downloader/
├── main.py, dependencies.py      # FastAPI app, lifespan, single BrowserRenderer/EgressPolicy wiring
├── ports.py, errors.py           # Framework-free Protocols (BrowserRenderer, JobStore, WorkQueue) & error table
├── config.py, runtime_config.py  # Pydantic Settings (60+ env vars)
├── auth.py, drain.py             # Settings-only compare_digest auth; SIGTERM pre-stop & 503 drain middleware
├── validation.py, ssrf_transport.py, http_client.py, content_encoding.py
│                                 # EgressPolicy, DNS-pinned httpx transport, per-download cookies, bounded decompression
├── browser/                      # manager.py (BrowserPool), session.py (BrowserSession), renderer.py, egress_proxy.py, actions.py
├── pdf_generator.py              # Shared PlaywrightPDFGenerator pool facade & PDF/screenshot capture
├── content_converter.py, transformers/
│                                 # Pure HTML heuristics, single-parse extractor, markdown/text & dedicated-pool OCR
├── crawling/                     # RFC 9309 robots.py (60s negative TTL) & bounded Lua throttle.py
├── routes/                       # download, render, batch, schedules, metrics (thin HTTP adapters)
├── services/                     # Pure convert() core, content_negotiation, multi_format, batch_processor, webhooks
├── scheduler/, job_manager.py    # APScheduler (off-loop store) & atomic Redis batch MULTI tracking
├── worker.py, worker_runtime.py, worker_queue.py
│                                 # Split worker BLMOVE queue, Lua reclaim, browser RPC, heartbeats & metrics
├── models/, telemetry.py, logging_config.py, middleware.py, ratelimit_middleware.py
monitoring/                       # prometheus.yml, alerting_rules.yml, docker-compose.monitoring.yml
sdks/                             # Typed Python, JavaScript/TypeScript, and Go clients
```

## API & Wire Contracts

Full endpoint schemas live in [`spec/contracts.md`](spec/contracts.md) (`GET /health`, `GET /{url}`, `POST /render`, `POST /batch`, `/jobs/{job_id}/{status,results}`, `DELETE /jobs/{job_id}`, `/schedules` CRUD + `/history`, `/metrics*`).

- **Single format (`Accept` omitted or 1 MIME):** Returns raw bytes with matching `Content-Type` (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, `application/json`). Single `Accept: text/html` returns raw HTML, **not** JSON.
- **Multi-format (`2+` MIME types):** Returns JSON mapping each requested MIME type to its content (adding `"errors": {...}` only on partial failure).
- **URL encoding & media guard:** Percent-encode `%`, `?`, and `#` in `{url}` on `GET /{url}` (`%25`, `%3F`, `%23`; SDKs do this automatically). Undocumented query keys return `HTTP 400` (`validation_error`); non-text binary media (`audio/*`, `video/*`, `application/octet-stream`, archives) returns `HTTP 415` on text/HTML/Markdown/multi-format paths.

## Testing Strategy

10-marker pytest suite (`smoke`, `unit`, `integration`, `e2e`, `port`, `docker_compose`, `slow`, `network`, `requires_redis`, `requires_playwright`; `>= 88%` overall and `>= 75%` per-critical-module branch coverage enforced offline, plus SDK suites in `tests/smoke/test_sdk_suites.py` and `ast-grep` contract checks in `tests/test_isolation_guard.py`):
- **`smoke`**: Auth, SSRF, rate limits, governance & ADR-005 link hygiene (`<15s`, `~106` tests including SDK & AST checks)
- **`unit`**: Pure modules, Ports/Adapters parity, mocked routes (`tests/conftest.py` offline DNS guard)
- **`integration`**: Real Chromium SSRF redirect (`requires_playwright`) and Redis queue (`requires_redis`)
- **`e2e`**: Zero-internet loopback HTTP -> FastAPI -> Playwright Chromium (`tests/e2e/test_loopback_e2e.py`, blocking in `cloudbuild.yaml`) plus opt-in Docker Compose / `@pytest.mark.network` suites

### Escape-Analysis Gate (E2E / Prod -> Unit)
Every E2E, integration, or production finding (e.g., `F-2026-11-20-001`) MUST complete this checklist before closing:
1. Classify why offline unit/fake suites missed it: `missing_unit_case | fake_drift | env_parity`.
2. Land a failing-first unit regression test (for `missing_unit_case` or `fake_drift`) or loopback-Chromium E2E scenario (for `env_parity` / driver RPC interactions) covering the root cause.
3. Verify the regression fails without the fix and passes under `gate test downloader` (and Cloud Build `downloader:ci` when browser/Redis runtime is involved).

## Environment & Security Summary

All defaults live in [`.env.example`](.env.example) and [`spec/contracts.md`](spec/contracts.md):
- **Auth & Docs:** `DOWNLOADER_KEY` (Settings-only `secrets.compare_digest` on all routes except `GET /health`, including `/metrics*`; required when `ENVIRONMENT=production` unless `AUTH_ALLOW_UNAUTHENTICATED_PRODUCTION=true`). Production disables `/docs`, `/redoc`, and `/openapi.json` (`404`). Default `CORS_ALLOWED_ORIGINS` is localhost-only.
- **SSRF & Size Caps:** `SSRF_BLOCK_PRIVATE_IPS=true` (async DNS + pinned `SSRFSafeHTTPTransport` with `trust_env=False`, `3xx` redirect checks, `BrowserEgressProxy`, `service_workers="block"`, `accept_downloads=False`, and `context.route("**/*", ...)`). `CONTENT_MAX_DOWNLOAD_SIZE=52428800` (`50 MB` -> `413` on HTTP streams and rendered browser outputs).
- **Crawling & Rate Limits:** `RESPECT_ROBOTS_TXT=true`, `USER_AGENT`, `MAX_CRAWL_DELAY=30.0` (RFC 9309 `*`/`$` + 60s negative TTL; bounded waits -> `429 Retry-After`; Playwright uses `Chrome/120` UA unless `USER_AGENT` is set while `robots.txt` uses `REST-API-Downloader/1.0`). `RATELIMIT_ENABLED=true` + `RATELIMIT_TRUSTED_PROXIES` (auth vs unauth buckets).
- **Runtime & Ops:** `REDIS_URI`, `PDF_POOL_SIZE=3`, `PDF_CONCURRENCY<=PDF_POOL_SIZE`, `PDF_WAIT_UNTIL=domcontentloaded`, `OCR_ENABLED=true` (`OCR_MAX_IMAGE_PIXELS=25000000`, dedicated `downloader-ocr` pool), `WEBHOOK_ENABLED=true` (HMAC-SHA256, sealed via dedicated `WEBHOOK_SECRET_KEY` when `REDIS_URI` or split-worker mode is set), `SHUTDOWN_DRAIN_TIMEOUT=30.0`, `SHUTDOWN_PRE_STOP_DELAY=2.0` (fails `/health` `503` during pre-stop while still serving live traffic; set Kubernetes `terminationGracePeriodSeconds >= 40`), `DEPLOYMENT_MODE=all|api|worker`, `WORKER_MAX_ATTEMPTS=3`, `OTEL_ENABLED=false`, non-root `:8000`.

## Planning & Backlog Workflow (Roadmap-First, No `TODO.md`)

Upcoming milestones and unscheduled backlog live in [`docs/roadmap.md`](docs/roadmap.md). Never create `TODO.md`; `spec/` stays status-free; archive completed milestones via `gate roadmap-archive downloader --done` (checked by `--check`).

| Path | Purpose |
|------|---------|
| [`docs/roadmap.md`](docs/roadmap.md) | Upcoming milestones, unscheduled backlog, and `## Shipped` summaries |
| [`docs/archive/roadmap-history.md`](docs/archive/roadmap-history.md) | Archived milestone implementation history |
| [`DECISIONS.md`](DECISIONS.md) | Append-only architectural decision log (`<= 25 KB`) |
| [`CRITICAL.md`](CRITICAL.md) | Security-, contract-, and CI-critical paths (mandatory review before commit) |
| [`spec/contracts.md`](spec/contracts.md) | Status-free, **protected** public wire contracts (propose edits before changing) |
| `docs/` | Guides (`docs/guides/*.md`) & completed feature baselines (`docs/features/*.md`) |

### Verification Ladder
- **T1 (commit gate):** `gate test downloader` (`uv lock --check`, `ruff`, `mypy src/ tests/`, offline `pytest -n auto --cov=src/downloader --cov-fail-under=88` + `scripts/check_critical_coverage.py` with `ast-grep` and Python/JS/Go SDK tests).
- **T2 (milestone gate):** Run relevant `integration` / Redis / Playwright suites, archive shipped milestones (`gate roadmap-archive downloader --done`), and log decisions in `DECISIONS.md`.
- **T3 (release gate):** Full container + Trivy `os,library` + real Redis/Chromium CI (`cloudbuild.yaml` / `ship downloader`).

## Common Gotchas

- **Streaming `httpx.MockTransport` tests:** `httpx.Response(200, content=...)` eagerly consumes the stream in `__init__`; pass `stream=httpx.ByteStream(raw_bytes)` when testing raw wire streaming or decompression bombs (`src/downloader/content_encoding.py`).
- **Browser SSRF egress pinning:** `BrowserPool` pairs loopback `BrowserEgressProxy` connect-time IP pinning with `service_workers="block"`, `install_ssrf_route_guard`, and post-navigation `validate_browser_navigation` on `response.request.redirected_from` + `page.url`.
- **Lifespan OTel & APScheduler `RedisJobStore`:** Reset `app.middleware_stack = None` around `FastAPIInstrumentor.(un)instrument_app(app)` and keep `excluded_urls` host-anchored (`^https?://[^/]+/(health|metrics)(\?.*)?$`). Pass `connection_pool=ConnectionPool.from_url(redis_uri)` to APScheduler `RedisJobStore` (rejects `url=`).
- **Cloud Build CI, Trivy & Scratch E2E:** Always run `uv lock --no-config --default-index https://pypi.org/simple` (`--offline` for version bumps; enforced by `test_uv_lock_uses_public_pypi_only`). Invoke TypeScript as `npx --yes --package=typescript@5.8.2 tsc --noEmit`; keep `Dockerfile` stripping unused `pip`/`setuptools`/`ensurepip` for Trivy. Submit zero-push scratch E2E builds strictly to `--project=btv-scratch-01` (never prod `sites-470716`), redirect `docker logs` before `grep -q` to avoid `SIGPIPE` (`141`), and set `WORKER_REMOTE_BROWSER_ENABLED=true` on both `api` and `worker` when testing remote RPC.
- **Secrets baseline (`.secrets.baseline`):** Regenerate via `uv run --no-sync detect-secrets scan --baseline .secrets.baseline` instead of hand-editing.

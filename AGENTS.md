# Downloader — Agent Instructions (`AGENTS.md`)

This file is the canonical operating brief for all coding agents (**Antigravity / `agy`**, **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Cursor**, **Gemini CLI**) working in this repository. `CLAUDE.md` is a relative symlink to `AGENTS.md`.

## Project Overview

REST API Downloader — a high-performance FastAPI web service for programmatic URL content downloading with intelligent article extraction, stateless-first ethical crawling (`robots.txt` + `Crawl-delay`), headless Chromium rendering, and Redis-backed batch/scheduled jobs.

- **Version:** 0.7.1
- **Stack:** Python 3.13+, FastAPI, httpx, Redis, Playwright, pytesseract/Pillow, OpenTelemetry (opt-in)
- **Registry:** `us-central1-docker.pkg.dev/sites-470716/containers/downloader` (Cloud Build CI/CD via [`cloudbuild.yaml`](cloudbuild.yaml) + GKE Autopilot / Flux CD GitOps)
- **Production domain:** `https://download.tvaroska.sk`

## Development Commands

```bash
# Hydrate locked environment + pre-commit hooks
uv sync --frozen
just install          # uv sync --upgrade + pre-commit install

# Run development server (requires Redis for batch, schedules & split workers)
just dev              # Uvicorn with hot-reload on port 8000
just worker           # Run standalone split worker (DEPLOYMENT_MODE=worker)
just split            # Launch Redis + API + Playwright/scheduler worker via Docker Compose

# Lint, format, AST rules, and type-check
just fmt                                                     # ruff check --fix + ruff format
uv run --no-sync ruff check src/ tests/ examples/
uv run --no-sync ruff format --check src/ tests/ examples/
ast-grep scan --rule specs/ast.rules.yaml .
uv run --no-sync mypy src/ tests/

# Fast offline verification gate (smoke + unit + hygiene + >=83% coverage, no Redis/Chromium needed)
uv run --no-sync pytest --cov=src/downloader -m "not (integration or e2e or network or requires_redis or requires_playwright)"
uv run --no-sync pytest -m "smoke or unit" -v

# Verify typed SDK suites (Python, JavaScript/TypeScript, Go)
uv run --no-sync pytest sdks/python/tests -c sdks/python/pyproject.toml -v
node --test sdks/javascript/test/client.test.mjs
go -C sdks/go vet ./... && go -C sdks/go test -v ./...

# Run targeted test tiers
just test                                       # Full pytest suite
uv run --no-sync pytest -m smoke -v             # Fast smoke + repo hygiene checks (<3s)
uv run --no-sync pytest -m integration -v       # Component & Playwright integration tests
uv run --no-sync pytest -m e2e -v               # Full Docker Compose + Redis E2E suite
uv run --no-sync pytest --cov=src --cov-report=html

# Build and push Docker image to Artifact Registry
just docker           # Builds + pushes us-central1-docker.pkg.dev/sites-470716/containers/downloader

# End-to-end Docker test
just e2e              # Build image, start Redis + downloader, run examples/*.py

# Clean build artifacts
just clean
```

## Architecture

```
src/downloader/
├── main.py                  # FastAPI app, lifespan, single BrowserRenderer/EgressPolicy wiring
├── ports.py                 # BrowserRenderer, JobStore, WorkQueue Protocols + local/remote/in-memory adapters
├── errors.py                # Single classify_domain_error exception-to-HTTP/batch/schedule table
├── config.py                # Pydantic Settings (60+ configurable params)
├── runtime_config.py        # OCR, Webhook, Shutdown, Worker & OpenTelemetry configs
├── auth.py                  # API key authentication (Bearer / X-API-Key via secrets.compare_digest)
├── drain.py                 # DrainCoordinator & DrainMiddleware (503 service_draining)
├── telemetry.py             # Opt-in OpenTelemetry SDK, auto-instrumentation & W3C propagation
├── worker.py                # Standalone split worker entrypoint (DEPLOYMENT_MODE=worker)
├── worker_queue.py          # Reliable Redis BLMOVE batch queue, browser RPC & heartbeats
├── validation.py            # EgressPolicy, canonical origin/host helpers & async DNS/IP SSRF protection
├── ssrf_transport.py        # DNS-pinned httpcore network backend & httpx transport (trust_env=False)
├── http_client.py           # Async httpx client, injected transport, redirect policy & streaming size guard
├── pdf_generator.py         # BrowserSession context manager, modal closer, size cap & PDF/screenshot capture
├── job_manager.py           # Redis-based batch job tracking, drain wait & post-persist webhooks
├── logging_config.py        # Structured JSON logging (path/method/status_code + trace_id/span_id)
├── middleware.py             # Metrics collection & X-Trace-ID header middleware
├── ratelimit_middleware.py   # Moving-window rate limiting enforcement (HTTP 429)
├── crawling/
│   ├── robots.py            # RFC 9309 */$ robots.txt evaluator, short negative TTL & byte-bounded cache
│   └── throttle.py          # Bounded Crawl-delay pacing (429 Retry-After, canonical host, atomic Redis Lua)
├── routes/
│   ├── download.py          # GET /{url} - maps pure convert() results & typed errors to HTTP
│   ├── render.py            # POST /render - scripted Playwright capture via convert()
│   ├── batch.py             # POST /batch (actions + webhooks), job status/results/cancel via JobStore/WorkQueue
│   ├── schedules.py         # Cron schedule CRUD (screenshot + webhooks) & execution history
│   └── metrics.py           # Prometheus, performance, health-score & live metrics
├── services/
│   ├── content_processor.py # Pure convert(request, deps) -> ConversionResult (zero FastAPI imports)
│   ├── content_negotiation.py # Pure FORMAT_MIME_SPECS table & Accept header parsers
│   ├── multi_format.py      # Pure parallel multi-format conversion (single DOM render, forwards force_ocr)
│   ├── batch_processor.py   # Per-URL & background batch runner delegating to convert()
│   └── webhooks.py          # SSRF-safe HMAC-SHA256 webhook delivery with bounded retries
├── content_converter.py     # HTML heuristics, BrowserSession rendering & single-parse conversion
├── scheduler/               # APScheduler for scheduled downloads delegating to convert()
├── browser/                 # Playwright BrowserPool (accept_downloads=False), egress_proxy & actions.py
├── models/                  # Pydantic request/response schemas
└── transformers/            # common.py single-parse extractor, markdown, plaintext & dedicated-pool OCR

sdks/
├── python/                  # Typed sync & async Python client (httpx + Pydantic v2)
├── javascript/              # Typed TypeScript/ESM client (built-in fetch, zero runtime deps)
└── go/                      # Idiomatic typed Go client (net/http stdlib, zero external deps)
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with version, deployment mode, auth, worker stats (`503` while draining) |
| GET | `/{url}` | Download URL with `Accept` negotiation (`?render=`, `?wait_for=`, `?output=screenshot\|pdf`, `?full_page=`, `?screenshot_format=`, `?actions=`, `?ocr=`, `?ocr_lang=`) |
| POST | `/render` | Scripted headless browser capture with allowlisted Playwright `actions` |
| POST | `/batch` | Submit background batch download job with optional `actions` and `webhook_url` (requires Redis) |
| GET | `/jobs/{job_id}/status` | Check batch job progress |
| GET | `/jobs/{job_id}/results` | Download completed batch job results |
| DELETE | `/jobs/{job_id}` | Cancel running or pending batch job |
| POST | `/schedules` | Create scheduled download with optional `webhook_url` (requires Redis) |
| GET | `/schedules` | List configured schedules |
| GET | `/schedules/{id}` | Get schedule details |
| GET | `/schedules/{id}/history` | Paginated schedule execution history (`?limit=20&offset=0`) |
| DELETE | `/schedules/{id}` | Delete schedule |
| GET | `/metrics` | Prometheus text exposition metrics |
| GET | `/metrics/performance` | JSON uptime, request counts, error rates, and p95 latency summary |
| GET | `/metrics/health-score` | Computed system health score (`0–100`) and contributing factors |
| GET | `/metrics/live` | Real-time semaphore concurrency (PDF/batch) and connection pool stats |

**`Accept` Content Negotiation & URL Encoding Wire Contract (`GET /{url}`):**
- **Single format (`Accept` omitted or 1 MIME type):** Returns raw payload bytes directly with the matching `Content-Type` — `text/plain` (default), `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, or `application/json` (base64 content + metadata). Callers sending `Accept: text/html` receive raw HTML bytes (`response.text`), **not** JSON.
- **Multi-format (`2+` MIME types, e.g. `Accept: text/html, text/markdown`):** Returns `application/json` mapping each requested MIME type to its rendered body (`{"text/html": "...", "text/markdown": "..."}`, including `"errors": {...}` only when one or more requested formats fail).
- **Target URL encoding & binary media guard:** Target URLs containing `%`, `?`, or `#` on `GET /{url}` must percent-encode them in the `{url}` path (`%25`, `%3F`, `%23`; all three SDKs encode automatically), and undocumented outer query parameters return `HTTP 400` (`validation_error`). Non-text binary `Content-Type`s (`audio/*`, `video/*`, `application/octet-stream`, archives) return `HTTP 415` (`unsupported_media_type`) on text/HTML/Markdown/multi-format conversion paths.

## Testing Strategy

4-marker pytest suite (`935` collected, `905` fast offline gate tests across `smoke` and `unit` at `>= 83%` coverage including Python/JS/Go SDK gates in `tests/smoke/test_sdk_suites.py`):
- **Smoke (`-m smoke`)**: Auth, SSRF/validation, rate limits, server startup, governance & ADR-005 link hygiene (`<3s`)
- **Unit (`-m unit`)**: Isolated modules, Ports/Adapters parity, middleware, browser pool, job manager, and mocked routes (`tests/conftest.py` fixture DNS)
- **Integration (`-m integration`)**: Real Playwright PDF/HTML rendering and browser security isolation
- **E2E (`-m e2e`)**: Full Docker Compose + Redis + live example verification

## Environment & Security Summary

Full defaults live in [`.env.example`](.env.example) and [`pyproject.toml`](pyproject.toml):
- **Auth & Docs:** `DOWNLOADER_KEY` (constant-time `secrets.compare_digest` via `Bearer` or `X-API-Key` across all routes except `GET /health`, including `/metrics*`); `ENVIRONMENT=production` disables `/docs`, `/redoc`, and `/openapi.json` (`HTTP 404`); localhost-only default `CORS_ALLOWED_ORIGINS`.
- **SSRF & Output Caps:** `SSRF_BLOCK_PRIVATE_IPS=true` (async DNS resolution + DNS-pinned `SSRFSafeHTTPTransport` with `trust_env=False`, `3xx` redirect re-validation, `BrowserEgressProxy`, `service_workers="block"`, `accept_downloads=False`, and `context.route("**/*", ...)` blocking private/loopback/shared-space/`169.254.169.254`/`file://`); `CONTENT_MAX_DOWNLOAD_SIZE=52428800` (`50 MB` -> `HTTP 413` on HTTP streams and rendered HTML/PDF/screenshots).
- **Ethical Crawling & Rate Limits:** `RESPECT_ROBOTS_TXT=true`, `USER_AGENT`, `ROBOTS_CACHE_TTL=3600`, `ROBOTS_CACHE_MAX_SIZE=1000`, `ROBOTS_FETCH_TIMEOUT=5.0`, `MAX_CRAWL_DELAY=30.0` (RFC 9309 `*`/`$` + longest-match, 60s negative failure TTL, bounded `Crawl-delay` wait -> `429 Retry-After`; Playwright sends a hard-coded `Chrome/120` UA unless `USER_AGENT` is explicitly set, while `robots.txt` is evaluated for the default `REST-API-Downloader/1.0` agent, `src/downloader/pdf_generator.py:33-76`); `RATELIMIT_ENABLED=true` and `RATELIMIT_TRUSTED_PROXIES` (`60/min` download, `20/min` `/batch`, `200/min` status/`/metrics*`/`/jobs/*`, `100/min` default, auth vs unauth bucket isolation). Scheduled downloads keep only output `content_size` (`src/downloader/scheduler/executor.py:236-253`).
- **Runtime, Browser, OCR, Webhooks & Split Workers:** `REDIS_URI`, `HTTP_MAX_CONNECTIONS=200`, `HTTP_REQUEST_TIMEOUT=30`, `BATCH_CONCURRENCY`, `BATCH_MAX_URLS_PER_BATCH=50`, `PDF_CONCURRENCY`, `PDF_MAX_USES_PER_BROWSER=50`, `PDF_IDLE_TIMEOUT=600`, `OCR_ENABLED=true` (`OCR_DEFAULT_LANGUAGE=eng`, `OCR_MAX_PIXELS=25000000`, dedicated `downloader-ocr` pool), `WEBHOOK_ENABLED=true` (post-persistence HMAC-SHA256), `SHUTDOWN_DRAIN_TIMEOUT=30.0`, `DEPLOYMENT_MODE=all|api|worker`, `OTEL_ENABLED=false`, non-root container on `:8000`.

## Planning & Backlog Workflow (Roadmap-First, No `TODO.md`)

All upcoming milestones, open `- [ ]` tasks, and unscheduled backlog items live in [`docs/roadmap.md`](docs/roadmap.md). Never create a `TODO.md`; `spec/` stays status-free, and shipped milestones or completed `- [x]` items move into [`docs/archive/roadmap-history.md`](docs/archive/roadmap-history.md) via `proj-test roadmap-archive downloader --done` (verified with `proj-test roadmap-archive downloader --check`).

### Layout

| Path | Purpose |
|------|---------|
| [`docs/roadmap.md`](docs/roadmap.md) | Ordered upcoming release milestones (`- [ ]` tasks), unscheduled backlog (plain bullets/tables), and `## Shipped` one-liners |
| [`docs/archive/roadmap-history.md`](docs/archive/roadmap-history.md) | Archived milestone and release implementation history moved out of `docs/roadmap.md` |
| [`DECISIONS.md`](DECISIONS.md) | Append-only architectural decision and learning log (`<= 25 KB`) |
| [`CRITICAL.md`](CRITICAL.md) | Registry of security-, contract-, and CI-critical paths (mandatory review before commit) |
| `spec/` | THE WHAT — requirements and public wire contracts ([`spec/contracts.md`](spec/contracts.md)). Status-free, **protected** |
| `docs/` | Guides & completed feature baselines — `docs/guides/*.md`, `docs/features/*.md` (e.g., [`docs/features/ethical-crawling.md`](docs/features/ethical-crawling.md)) |

### Verification Ladder
- **T1 (every change — gates commit):** `proj-test downloader` (`ast-grep`, `ruff check`, `ruff format --check`, `mypy src/ tests/`, and offline fast `pytest --cov=src/downloader` suite).
- **T2 (gates milestone completion in `docs/roadmap.md`):** Exercise the changed behavior against acceptance criteria (including `integration` / `requires_playwright` / Redis suites and SDK suites when relevant); move shipped milestones into `docs/archive/roadmap-history.md` (`proj-test roadmap-archive downloader --done`) and record decisions in `DECISIONS.md`.
- **T3 (release gate):** Full container + Trivy `os,library` + Playwright smoke verification (`cloudbuild.yaml` / `proj-release downloader`) against [`spec/contracts.md`](spec/contracts.md).

### `CRITICAL.md` Escalation
Any change touching a path listed in [`CRITICAL.md`](CRITICAL.md) requires explicit security/contract review before commit. `spec/` is protected — propose changes to `spec/contracts.md` for review rather than editing it unprompted.

## Common Gotchas

- **Streaming `httpx.MockTransport` tests:** `httpx.Response(200, content=...)` eagerly runs `self.read()` in `__init__` (`is_stream_consumed == True`, wrapping `ByteStream` inside `BoundAsyncStream`). Pass `stream=httpx.ByteStream(raw_bytes)` whenever testing raw wire streaming or bounded decompression bombs (`src/downloader/content_encoding.py`).
- **Browser SSRF egress pinning:** Playwright `BrowserPool` routes Chromium egress through loopback connect-time IP-pinning proxy `BrowserEgressProxy` (`src/downloader/browser/egress_proxy.py`), sets `service_workers="block"`, attaches `install_ssrf_route_guard`, and re-validates every hop in `response.request.redirected_from` + `page.url` (`validate_browser_navigation`).
- **Lifespan OTel & APScheduler `RedisJobStore`:** Calling `FastAPIInstrumentor.instrument_app(app)` inside `lifespan` must reset `app.middleware_stack = None` on both instrument and uninstrument so Starlette rebuilds middleware, and `excluded_urls` must stay host-anchored (`^https?://[^/]+/(health|metrics)(\?.*)?$`) so downloading target URLs ending in `/health` or `/metrics` remains traced (`src/downloader/telemetry.py`). APScheduler 3.x `RedisJobStore` forwards kwargs to `redis.Redis.__init__` (which rejects `url=`), so `src/downloader/scheduler/service.py` must pass `connection_pool=ConnectionPool.from_url(redis_uri)`.
- **Cloud Build CI, Trivy & Scratch E2E:** Always run `uv lock --no-config --default-index https://pypi.org/simple` (`--offline` for version bumps) so `uv.lock` stays pinned to public PyPI (`test_uv_lock_uses_public_pypi_only`). In `cloudbuild.yaml`, invoke TypeScript via `npx --yes --package=typescript@5.8.2 tsc --noEmit` (bare `npx typescript@...` fails because `typescript` exposes both `tsc` and `tsserver`), and strip unused base-image `pip`/`setuptools`/`ensurepip` in `Dockerfile` so Trivy `--pkg-types os,library` stays clean. When Docker is unavailable locally, submit zero-push E2E builds strictly against `--project=btv-scratch-01` (never prod `sites-470716`), redirect `docker logs <ctr> > /tmp/ctr.log 2>&1` before `grep -q` (avoids `SIGPIPE` exit `141` under `pipefail`), and set `WORKER_REMOTE_BROWSER_ENABLED=true` on both `api` and `worker` containers when testing remote browser RPC.
- **Secrets baseline (`.secrets.baseline`):** Regenerate via `uv run --no-sync detect-secrets scan --baseline .secrets.baseline` rather than hand-editing hashes or line numbers.

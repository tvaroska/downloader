# Downloader — Agent Instructions (`AGENTS.md`)

This file is the canonical operating brief for all coding agents (**Antigravity / `agy`**, **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Cursor**, **Gemini CLI**) working in this repository. `CLAUDE.md` is a relative symlink to `AGENTS.md`.

## Project Overview

REST API Downloader — a high-performance FastAPI web service for programmatic URL content downloading with intelligent article extraction, stateless-first ethical crawling (`robots.txt` + `Crawl-delay`), headless Chromium rendering, and Redis-backed batch/scheduled jobs.

- **Version:** 0.7.0
- **Stack:** Python 3.13+, FastAPI, httpx, Redis, Playwright, pytesseract/Pillow, OpenTelemetry (opt-in)
- **Registry:** `us-central1-docker.pkg.dev/sites-470716/containers/downloader` (Cloud Build CI/CD via [`cloudbuild.yaml`](cloudbuild.yaml) + GKE Autopilot / Flux CD GitOps)
- **Production domain:** `https://download.tvaroska.sk`
- **Operational Runbook Skill:** [`skills/downloader-ops/SKILL.md`](skills/downloader-ops/SKILL.md) (exposed via `.agents/skills` and `.claude/skills`)

## Development Commands

```bash
# Hydrate locked environment + pre-commit hooks
uv sync --frozen
just install          # uv sync --upgrade + pre-commit install

# Run development server (requires Redis for batch, schedules & split workers)
just dev              # Uvicorn with hot-reload on port 8000
just worker           # Run standalone split worker (DEPLOYMENT_MODE=worker)
just split            # Launch Redis + API + Playwright/scheduler worker via Docker Compose

# Lint, format, and type-check
just fmt                                                     # ruff check --fix + ruff format
uv run --no-sync ruff check src/ tests/ examples/
uv run --no-sync ruff format --check src/ tests/ examples/
uv run --no-sync mypy src/

# Fast offline verification gate (smoke + unit + hygiene, no Redis/Chromium needed)
uv run --no-sync pytest -m "not (integration or e2e or network or requires_redis or requires_playwright)"
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
├── main.py                  # FastAPI app, lifespan, drain/OTel/middleware setup
├── config.py                # Pydantic Settings (60+ configurable params)
├── runtime_config.py        # OCR, Webhook, Shutdown, Worker & OpenTelemetry configs
├── auth.py                  # API key authentication (Bearer / X-API-Key)
├── drain.py                 # DrainCoordinator & DrainMiddleware (503 service_draining)
├── telemetry.py             # Opt-in OpenTelemetry SDK, auto-instrumentation & W3C propagation
├── worker.py                # Standalone split worker entrypoint (DEPLOYMENT_MODE=worker)
├── worker_queue.py          # Reliable Redis BLMOVE batch queue, browser RPC & heartbeats
├── validation.py            # URL & custom header sanitization + async DNS/IP SSRF protection
├── ssrf_transport.py        # DNS-pinned httpcore network backend & httpx transport (trust_env=False)
├── http_client.py           # Async httpx client, redirect SSRF & streaming size guard
├── pdf_generator.py         # Lazy Playwright PDF & screenshot generator, idle reaper & SSRF guard
├── job_manager.py           # Redis-based batch job tracking, drain wait & post-persist webhooks
├── logging_config.py        # Structured JSON logging (path/method/status_code + trace_id/span_id)
├── middleware.py             # Metrics collection & X-Trace-ID header middleware
├── ratelimit_middleware.py   # Moving-window rate limiting enforcement (HTTP 429)
├── crawling/
│   ├── robots.py            # Stateless-first robots.txt evaluator + TTL/LRU/Redis cache
│   └── throttle.py          # Per-domain Crawl-delay pacing (in-memory + optional Redis)
├── routes/
│   ├── download.py          # GET /{url} - single/multi-format URL download, screenshot/PDF, OCR, actions
│   ├── render.py            # POST /render - scripted Playwright capture (markdown/text/html/pdf/screenshot)
│   ├── batch.py             # POST /batch (actions + webhooks), job status/results/cancel
│   ├── schedules.py         # Cron schedule CRUD (screenshot + webhooks) & execution history
│   └── metrics.py           # Prometheus, performance, health-score & live metrics
├── services/
│   ├── content_processor.py # Format handlers (text, markdown, HTML, PDF, screenshot, OCR, JSON)
│   ├── content_negotiation.py # Accept header parser, screenshot response & remote worker proxy
│   ├── multi_format.py      # Multi-format Accept JSON response builder
│   ├── batch_processor.py   # Per-URL & background batch runner + post-persistence webhook trigger
│   └── webhooks.py          # SSRF-safe HMAC-SHA256 webhook delivery with bounded retries
├── content_converter.py     # HTML parsing, single-pass Playwright settle, actions & OCR integration
├── scheduler/               # APScheduler for scheduled downloads (paused CRUD in api mode)
├── browser/                 # Playwright BrowserPool isolation, hardening & allowlisted actions.py
├── models/                  # Pydantic request/response schemas
└── transformers/            # Format converters (markdown, plaintext, OCR)

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

**`Accept` Content Negotiation Wire Contract (`GET /{url}`):**
- **Single format (`Accept` omitted or 1 MIME type):** Returns raw payload bytes directly with the matching `Content-Type` — `text/plain` (default), `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, or `application/json` (base64 content + metadata). Callers sending `Accept: text/html` receive raw HTML bytes (`response.text`), **not** JSON.
- **Multi-format (`2+` MIME types, e.g. `Accept: text/html, text/markdown`):** Returns `application/json` mapping each requested MIME type to its rendered body (`{"text/html": "...", "text/markdown": "..."}`, including `"errors": {...}` only when one or more requested formats fail).

## Testing Strategy

4-marker pytest suite (`885+` total tests, `752+` fast offline tests across `smoke`, `unit`, `integration`, and `e2e`, plus standalone SDK test suites in `sdks/`):
- **Smoke (`-m smoke`)**: Fast critical-path checks (auth, validation/SSRF, rate limits, server startup, repo/ADR-005/link hygiene) in `<3s`
- **Unit (`-m unit`)**: Isolated module, middleware, browser-manager, job-manager, metrics, and mocked API endpoint tests (offline deterministic fixture DNS in `tests/conftest.py`)
- **Integration (`-m integration`)**: Real Playwright PDF generation, browser security isolation, and multi-component workflows
- **E2E (`-m e2e`)**: Full Docker Compose + Redis + live example verification

## Key Dependencies

- **FastAPI** + **uvicorn**: Async web framework
- **httpx[http2]**: Async HTTP client with connection pooling
- **beautifulsoup4** + **lxml** + **markdownify**: Content extraction and conversion
- **pytesseract** + **Pillow**: Image-to-text OCR extraction
- **playwright**: Headless Chromium rendering, screenshot, and PDF generation
- **redis**: Batch job management, schedule history, split worker queues, rate limiting
- **slowapi**: Rate limiting middleware
- **apscheduler**: Cron-based scheduled download jobs
- **opentelemetry-sdk**: Opt-in distributed tracing (`OTEL_ENABLED`)
- **pydantic-settings**: Environment configuration management

## Environment Configuration

Key variables (see `.env.example` for full list):

- `DOWNLOADER_KEY`: API key for authentication (optional; when set, required on all endpoints except `GET /health`, including `/metrics*`)
- `REDIS_URI`: Redis connection string (required for `/batch`, `/schedules`, and `DEPLOYMENT_MODE=api|worker`; optional shared cache/throttle for rate limits and `robots.txt`)
- `ENVIRONMENT`: `development` / `staging` / `production` (`/docs`, `/redoc`, and `/openapi.json` return `HTTP 404` when `production`)
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: `http://localhost:3000,http://localhost:8080,http://127.0.0.1:3000,http://127.0.0.1:8080`)
- `USER_AGENT`: Outbound HTTP & Playwright `User-Agent` (default: `REST-API-Downloader/1.0`)
- `RESPECT_ROBOTS_TXT`: Enforce `robots.txt` & `Crawl-delay` pre-flight across `GET /{url}`, `POST /render`, `POST /batch`, and `/schedules` (default: `true`; returns `HTTP 403` `robots_disallowed` when blocked, fails open when unreachable)
- `ROBOTS_CACHE_TTL` / `ROBOTS_CACHE_MAX_SIZE` / `ROBOTS_FETCH_TIMEOUT` / `MAX_CRAWL_DELAY`: `robots.txt` cache TTL (`3600s`), bounded LRU size (`1000`), fetch timeout (`5.0s`), and `Crawl-delay` cap (`30.0s`)
- `CONTENT_MAX_DOWNLOAD_SIZE`: Streaming byte cap (default: `52428800` / 50MB; returns `HTTP 413` `content_too_large`)
- `HTTP_MAX_CONNECTIONS`: Max HTTP connections (default: 200)
- `HTTP_REQUEST_TIMEOUT`: Request timeout in seconds (default: 30)
- `RATELIMIT_ENABLED` / `RATELIMIT_TRUSTED_PROXIES`: Enable moving-window rate limiting (default: `true`; returns `HTTP 429` + `Retry-After` + `X-RateLimit-*`) and trusted proxy CIDRs for right-to-left `X-Forwarded-For` client IP resolution
- `BATCH_CONCURRENCY` / `BATCH_MAX_URLS_PER_BATCH`: Max concurrent batch requests (default: CPU×8, max 50) and max URLs per batch submission (default: 50, hard ceiling: 1000)
- `PDF_CONCURRENCY` / `PDF_MAX_USES_PER_BROWSER` / `PDF_IDLE_TIMEOUT`: Max concurrent browser sessions across PDF, screenshot, and Playwright HTML rendering (default: CPU×2, max 12), browser recycling cap (`50`), and idle scale-to-zero timeout (`600s`)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private/loopback/link-local IPs across DNS-pinned `httpcore` sockets, `3xx` redirects, `robots.txt`, webhooks, `/schedules`, and Playwright requests (default: `true`)
- `OCR_ENABLED` / `OCR_DEFAULT_LANGUAGE` / `OCR_TIMEOUT_SECONDS` / `OCR_MAX_PIXELS`: Tesseract OCR controls (`true`, `eng`, `30.0s`, `25_000_000` decompression-bomb cap)
- `WEBHOOK_ENABLED` / `WEBHOOK_TIMEOUT_SECONDS` / `WEBHOOK_MAX_RETRIES`: Post-persistence SSRF-safe HMAC-SHA256 webhook delivery controls (`true`, `10.0s`, `3`)
- `SHUTDOWN_DRAIN_TIMEOUT` / `SHUTDOWN_POLL_INTERVAL`: Graceful shutdown active-request and job drain window (`30.0s`, `0.25s`)
- `DEPLOYMENT_MODE` / `WORKER_POLL_TIMEOUT` / `WORKER_BROWSER_TASK_TIMEOUT` / `WORKER_MAX_CONCURRENT_JOBS`: Split API/worker topology (`all` default, `api`, `worker`)
- `OTEL_ENABLED` / `OTEL_SERVICE_NAME` / `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_CONSOLE_EXPORTER`: Opt-in OpenTelemetry distributed tracing (`false` default)

## Security

- Defense-in-depth SSRF protection (async DNS resolution + DNS-pinned `SSRFSafeHTTPTransport` with `trust_env=False`, `3xx` redirect hop re-validation, `robots.txt` & `/schedules` validation, and Playwright `context.route("**/*", ...)` interception; blocks localhost, `*.localhost`, hex/octal/integer/IPv4-mapped-IPv6 private IPs, `file://`, and `169.254.169.254` cloud metadata)
- Stateless-first ethical crawling (`robots.txt` compliance across `GET /{url}`, `/batch`, and `/schedules`, atomic Redis Lua `Crawl-delay` throttling, `HTTP 403` `robots_disallowed`, bounded in-memory TTL/LRU cache)
- Streaming download size guard (`CONTENT_MAX_DOWNLOAD_SIZE` -> `HTTP 413` `content_too_large` before buffering oversized streams)
- Constant-time API key authentication (`Authorization: Bearer` or `X-API-Key` header via `secrets.compare_digest` across download, batch, schedule, and `/metrics*` endpoints; `/docs`, `/redoc`, and `/openapi.json` disabled in `ENVIRONMENT=production`)
- Localhost-only default CORS origins
- Active moving-window rate limiting (`60/min` download, `20/min` batch, `200/min` status/jobs/metrics, `100/min` default -> `HTTP 429` + `Retry-After`, right-to-left trusted-proxy `X-Forwarded-For` resolution, and auth vs unauth bucket isolation)
- Non-root Docker container listening on unprivileged port `:8000` with isolated, auto-recycling Playwright browser contexts

## Planning & Backlog Workflow (Roadmap-First)

Milestones and deferred backlog items live in [`docs/roadmap.md`](docs/roadmap.md) (optional root `TODO.md <= 15 KB` is created only during multi-task active sprints and removed once `0` open items remain); `spec/` stays status-free.

### Layout

| Path | Purpose |
|------|---------|
| [`docs/roadmap.md`](docs/roadmap.md) | Canonical release milestones, phase status, and deferred backlog |
| [`DECISIONS.md`](DECISIONS.md) | Append-only architectural decision and learning log (`<= 25 KB`) |
| [`CRITICAL.md`](CRITICAL.md) | Registry of security-, contract-, and CI-critical paths (mandatory review before commit) |
| [`skills/downloader-ops/SKILL.md`](skills/downloader-ops/SKILL.md) | Operational runbooks (`Accept` contract, offline verification, Redis/Playwright E2E, Cloud Build releases, `.secrets.baseline`) |
| `spec/` | THE WHAT — requirements and public wire contracts ([`spec/contracts.md`](spec/contracts.md)). Status-free, **protected** |
| `docs/` | Guides & completed sprint archives — `docs/guides/*.md`, `docs/features/*.md` (e.g., [`docs/features/ethical-crawling.md`](docs/features/ethical-crawling.md)) |
| `.claude/plans/` | Optional machine-local scratch plans (gitignored via `.claude/*`, while `.claude/skills` and `.claude/settings.json` stay tracked; archive lasting decisions in `docs/features/*.md` + `DECISIONS.md`, never link `.claude/plans/...`) |

### Verification Ladder
- **T1 (every change — gates commit):** `ruff check`, `ruff format --check` (or `just fmt`), `mypy src/`, and offline fast `pytest` suite (`not (integration or e2e or network or requires_redis or requires_playwright)`).
- **T2 (gates milestone completion in `docs/roadmap.md`):** Exercise the changed behavior against acceptance criteria (including `integration` / `requires_playwright` / Redis suites when relevant); archive completed sprint batches into `docs/features/*.md` + `DECISIONS.md`.
- **T3 (release gate):** Full container + Redis verification (`just e2e` / `cloudbuild.yaml`) against [`spec/contracts.md`](spec/contracts.md).

### `CRITICAL.md` Escalation
Any change touching a path listed in [`CRITICAL.md`](CRITICAL.md) requires explicit security/contract review before commit. `spec/` is protected — propose changes to `spec/contracts.md` for review rather than editing it unprompted.

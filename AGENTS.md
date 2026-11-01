# Downloader — Agent Instructions (`AGENTS.md`)

This file is the canonical operating brief for all coding agents (**Antigravity / `agy`**, **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Cursor**, **Gemini CLI**) working in this repository. `CLAUDE.md` is a relative symlink to `AGENTS.md`.

## Project Overview

REST API Downloader — a high-performance FastAPI web service for programmatic URL content downloading with intelligent article extraction, stateless-first ethical crawling (`robots.txt` + `Crawl-delay`), headless Chromium rendering, and Redis-backed batch/scheduled jobs.

- **Version:** 0.6.0
- **Stack:** Python 3.13+, FastAPI, httpx, Redis, Playwright
- **Registry:** `us-central1-docker.pkg.dev/sites-470716/containers/downloader` (Cloud Build CI/CD via [`cloudbuild.yaml`](cloudbuild.yaml) + GKE Autopilot / Flux CD GitOps)
- **Production domain:** `https://download.tvaroska.sk`
- **Operational Runbook Skill:** [`skills/downloader-ops/SKILL.md`](skills/downloader-ops/SKILL.md) (exposed via `.agents/skills` and `.claude/skills`)

## Development Commands

```bash
# Hydrate locked environment + pre-commit hooks
uv sync --frozen
just install          # uv sync --upgrade + pre-commit install

# Run development server (requires Redis for batch & schedules)
just dev              # Uvicorn with hot-reload on port 8000

# Lint, format, and type-check
just fmt                                                     # ruff check --fix + ruff format
uv run --no-sync ruff check src/ tests/ examples/
uv run --no-sync ruff format --check src/ tests/ examples/
uv run --no-sync mypy src/

# Fast offline verification gate (smoke + unit + hygiene, no Redis/Chromium needed)
uv run --no-sync pytest -m "not (integration or e2e or network or requires_redis or requires_playwright)"
uv run --no-sync pytest -m "smoke or unit" -v

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
├── main.py                  # FastAPI app, lifespan, middleware setup
├── config.py                # Pydantic Settings (45+ configurable params)
├── auth.py                  # API key authentication (Bearer / X-API-Key)
├── validation.py            # URL sanitization + DNS/IP SSRF protection
├── http_client.py           # Async httpx client, redirect SSRF & streaming size guard
├── pdf_generator.py         # Lazy Playwright PDF generator, idle reaper & SSRF route guard
├── job_manager.py           # Redis-based batch job tracking
├── logging_config.py        # Structured JSON logging (top-level path/method/status_code)
├── middleware.py             # Metrics collection middleware
├── ratelimit_middleware.py   # Moving-window rate limiting enforcement (HTTP 429)
├── crawling/
│   ├── robots.py            # Stateless-first robots.txt evaluator + TTL/LRU/Redis cache
│   └── throttle.py          # Per-domain Crawl-delay pacing (in-memory + optional Redis)
├── routes/
│   ├── download.py          # GET /{url} - single/multi-format URL download
│   ├── batch.py             # POST /batch, job status/results/cancel
│   ├── schedules.py         # Cron schedule CRUD + execution history
│   └── metrics.py           # Prometheus, performance, health-score & live metrics
├── services/
│   └── content_processor.py # Format handlers (text, markdown, HTML, PDF, JSON)
├── content_converter.py     # HTML parsing, article extraction & JS rendering
├── scheduler/               # APScheduler for scheduled downloads
├── browser/                 # Playwright BrowserPool isolation & hardening
├── models/                  # Pydantic request/response schemas
└── transformers/            # Format converters
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with service and auth status |
| GET | `/{url}` | Download URL with `Accept` content negotiation (`?render=true`, `?wait_for=`) |
| POST | `/batch` | Submit background batch download job (requires Redis) |
| GET | `/jobs/{job_id}/status` | Check batch job progress |
| GET | `/jobs/{job_id}/results` | Download completed batch job results |
| DELETE | `/jobs/{job_id}` | Cancel running or pending batch job |
| POST | `/schedules` | Create scheduled download (requires Redis) |
| GET | `/schedules` | List configured schedules |
| GET | `/schedules/{id}` | Get schedule details |
| GET | `/schedules/{id}/history` | Paginated schedule execution history (`?limit=20&offset=0`) |
| DELETE | `/schedules/{id}` | Delete schedule |
| GET | `/metrics` | Prometheus text exposition metrics |
| GET | `/metrics/performance` | JSON uptime, request counts, error rates, and p95 latency summary |
| GET | `/metrics/health-score` | Computed system health score (`0–100`) and contributing factors |
| GET | `/metrics/live` | Real-time semaphore concurrency (PDF/batch) and connection pool stats |

**`Accept` Content Negotiation Wire Contract (`GET /{url}`):**
- **Single format (`Accept` omitted or 1 MIME type):** Returns raw payload bytes directly with the matching `Content-Type` — `text/plain` (default), `text/markdown`, `text/html`, `application/pdf`, or `application/json` (base64 content + metadata). Callers sending `Accept: text/html` receive raw HTML bytes (`response.text`), **not** JSON.
- **Multi-format (`2+` MIME types, e.g. `Accept: text/html, text/markdown`):** Returns `application/json` mapping each requested MIME type to its rendered body (`{"text/html": "...", "text/markdown": "...", "errors": {...}}`).

## Testing Strategy

4-marker pytest suite (`764+` tests across `smoke`, `unit`, `integration`, and `e2e`, 85%+ coverage target):
- **Smoke (`-m smoke`)**: Fast critical-path checks (auth, validation/SSRF, rate limits, server startup, repo/ADR-005/link hygiene) in `<3s`
- **Unit (`-m unit`)**: Isolated module, middleware, browser-manager, job-manager, metrics, and mocked API endpoint tests (offline deterministic fixture DNS in `tests/conftest.py`)
- **Integration (`-m integration`)**: Real Playwright PDF generation, browser security isolation, and multi-component workflows
- **E2E (`-m e2e`)**: Full Docker Compose + Redis + live example verification

## Key Dependencies

- **FastAPI** + **uvicorn**: Async web framework
- **httpx[http2]**: Async HTTP client with connection pooling
- **beautifulsoup4** + **lxml** + **markdownify**: Content extraction and conversion
- **playwright**: Headless Chromium rendering and PDF generation
- **redis**: Batch job management, schedule history, rate limiting
- **slowapi**: Rate limiting middleware
- **apscheduler**: Cron-based scheduled download jobs
- **pydantic-settings**: Environment configuration management

## Environment Configuration

Key variables (see `.env.example` for full list):

- `DOWNLOADER_KEY`: API key for authentication (optional)
- `REDIS_URI`: Redis connection string (required for `/batch` and `/schedules`; optional shared cache/throttle for rate limits and `robots.txt`)
- `ENVIRONMENT`: `development` / `staging` / `production`
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: `http://localhost:3000,http://localhost:8080,http://127.0.0.1:3000,http://127.0.0.1:8080`)
- `USER_AGENT`: Outbound HTTP & Playwright `User-Agent` (default: `REST-API-Downloader/1.0`)
- `RESPECT_ROBOTS_TXT`: Enforce `robots.txt` & `Crawl-delay` pre-flight (default: `true`; returns `HTTP 403` `robots_disallowed` when blocked, fails open when unreachable)
- `ROBOTS_CACHE_TTL` / `ROBOTS_CACHE_MAX_SIZE` / `ROBOTS_FETCH_TIMEOUT` / `MAX_CRAWL_DELAY`: `robots.txt` cache TTL (`3600s`), bounded LRU size (`1000`), fetch timeout (`5.0s`), and `Crawl-delay` cap (`30.0s`)
- `CONTENT_MAX_DOWNLOAD_SIZE`: Streaming byte cap (default: `52428800` / 50MB; returns `HTTP 413` `content_too_large`)
- `HTTP_MAX_CONNECTIONS`: Max HTTP connections (default: 200)
- `HTTP_REQUEST_TIMEOUT`: Request timeout in seconds (default: 30)
- `RATELIMIT_ENABLED`: Enable moving-window rate limiting (default: `true`; returns `HTTP 429` + `Retry-After` + `X-RateLimit-*`)
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: CPU×8, max 50)
- `PDF_CONCURRENCY` / `PDF_MAX_USES_PER_BROWSER` / `PDF_IDLE_TIMEOUT`: Max concurrent PDFs (default: CPU×2, max 12), browser recycling cap (`50`), and idle scale-to-zero timeout (`600s`)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private/loopback/link-local IPs across direct, redirect, `robots.txt`, and Playwright requests (default: `true`)

## Security

- Defense-in-depth SSRF protection (pre-flight DNS resolution, `3xx` redirect hop re-validation, `robots.txt` validation, and Playwright `context.route("**/*", ...)` interception; blocks localhost, RFC1918 private IPs, `file://`, and `169.254.169.254` cloud metadata)
- Stateless-first ethical crawling (`robots.txt` compliance, `Crawl-delay` throttling, `HTTP 403` `robots_disallowed`, bounded in-memory TTL/LRU cache)
- Streaming download size guard (`CONTENT_MAX_DOWNLOAD_SIZE` -> `HTTP 413` `content_too_large` before buffering oversized streams)
- Optional API key authentication (`Authorization: Bearer` or `X-API-Key` header)
- Localhost-only default CORS origins
- Active moving-window rate limiting (`60/min` download, `20/min` batch, `200/min` status, `100/min` default -> `HTTP 429` + `Retry-After`)
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

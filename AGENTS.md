# Downloader — Agent Instructions (`AGENTS.md`)

This file is the canonical operating brief for all coding agents (**Antigravity / `agy`**, **Claude Code (`claude`)**, **OpenAI Codex (`codex`)**, **Cursor**, **Gemini CLI**) working in this repository. `CLAUDE.md` is a relative symlink to `AGENTS.md`.

## Project Overview

REST API Downloader — a high-performance FastAPI web service for programmatic URL content downloading with intelligent article extraction.

- **Version:** 0.5.0
- **Stack:** Python 3.10+, FastAPI, httpx, Redis, Playwright
- **Registries:** `us-central1-docker.pkg.dev/sites-470716/containers/downloader` (GKE Autopilot + Flux CD GitOps via [`cloudbuild.yaml`](cloudbuild.yaml)) and `ghcr.io/tvaroska/downloader`
- **Production domain:** `https://download.tvaroska.sk`

## Development Commands

```bash
# Install dependencies (uses uv package manager)
just install          # uv sync + pre-commit hooks

# Run development server (requires Redis)
just dev              # Uvicorn with hot-reload on port 8000

# Format and lint
just fmt              # ruff check --fix

# Run fast verification gate (lint + mypy + smoke & unit tests)
../bin/proj-test downloader
uv run pytest -m "smoke or unit" -v

# Run full test tiers
just test                         # pytest tests/ -v
uv run pytest -m smoke -v         # Smoke tests only (<3s)
uv run pytest -m integration -v   # Integration tests (~15s)
uv run pytest -m e2e -v           # E2E tests (~60s)
uv run pytest --cov=src --cov-report=html  # With coverage

# Build and push Docker image
just docker           # Builds + pushes to ghcr.io/tvaroska/downloader:{version}

# End-to-end Docker test
just e2e              # Build image, start Redis + downloader, run examples

# Clean build artifacts
just clean
```

## Architecture

```
src/downloader/
├── main.py                  # FastAPI app, lifespan, middleware setup
├── config.py                # Pydantic Settings (40+ configurable params)
├── auth.py                  # API key authentication (Bearer / X-API-Key)
├── http_client.py           # Async httpx client with connection pooling
├── pdf_generator.py         # Playwright-based PDF generation
├── job_manager.py           # Redis-based job tracking
├── middleware.py             # Metrics collection middleware
├── ratelimit_middleware.py   # Rate limiting enforcement
├── routes/
│   ├── download.py          # GET /{url} - single URL download
│   ├── batch.py             # POST /batch, job status/results/cancel
│   ├── schedules.py         # Schedule management (CRUD)
│   └── metrics.py           # Metrics endpoint
├── services/
│   └── content_processor.py # Format handlers (text, markdown, HTML, PDF, JSON)
├── content_converter.py     # HTML parsing and article extraction
├── scheduler/               # APScheduler for scheduled downloads
├── browser/                 # Playwright browser management
├── models/                  # Pydantic response schemas
└── transformers/            # Format converters
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check with service status |
| GET | `/{url}` | Download URL with content negotiation (Accept header) |
| POST | `/batch` | Submit batch download job |
| GET | `/jobs/{job_id}/status` | Check job progress |
| GET | `/jobs/{job_id}/results` | Download job results |
| DELETE | `/jobs/{job_id}` | Cancel job |
| POST | `/schedules` | Create scheduled download |
| GET | `/schedules` | List schedules |
| GET | `/schedules/{id}` | Get schedule details |
| DELETE | `/schedules/{id}` | Delete schedule |
| GET | `/metrics` | Service metrics |

**Content negotiation** via Accept header: `text/plain` (default), `text/markdown`, `text/html`, `application/pdf`, `application/json`

## Testing Strategy

3-tier test system with pytest markers:
- **Smoke** (<3s): 80 fast sanity checks
- **Unit**: Pure unit tests with no external dependencies
- **Integration** (~15s): Component testing
- **E2E** (~60s): Full system validation with Docker

248 total tests, 85%+ coverage target.

## Key Dependencies

- **FastAPI** + **uvicorn**: Async web framework
- **httpx[http2]**: Async HTTP client with connection pooling
- **beautifulsoup4** + **lxml** + **markdownify**: Content extraction and conversion
- **playwright**: Browser-based PDF generation
- **redis**: Job management, rate limiting, caching
- **slowapi**: Rate limiting middleware
- **apscheduler**: Scheduled download jobs
- **pydantic-settings**: Configuration management

## Environment Configuration

Key variables (see `.env.example` for full list):

- `DOWNLOADER_KEY`: API key for authentication (optional)
- `REDIS_URI`: Redis connection string
- `ENVIRONMENT`: development/staging/production
- `HTTP_MAX_CONNECTIONS`: Max HTTP connections (default: 200)
- `HTTP_REQUEST_TIMEOUT`: Request timeout in seconds (default: 30)
- `RATELIMIT_ENABLED`: Enable rate limiting (default: true)
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: CPU*8, max 50)
- `PDF_CONCURRENCY`: Max concurrent PDFs (default: CPU*2, max 12)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private IPs (default: true)

## Security

- SSRF protection (blocks localhost, private IPs)
- URL validation and sanitization
- Optional API key authentication (Bearer token or X-API-Key header)
- Multi-tier rate limiting (60/min download, 20/min batch, 200/min status)
- Non-root Docker container

## Planning Workflow (gen-3)

This project uses a sprint-based planning workflow managed through slash commands.
Live status lives in ONE place (`TODO.md`); `spec/` stays status-free.

### Layout

| Path | Purpose |
|------|---------|
| `TODO.md` | Tasks + live status/counts (the ONLY place status lives) |
| `DECISIONS.md` | Append-only decision/learning log (committed) |
| `CRITICAL.md` | Registry of protected/sensitive paths (auto-escalate) |
| `spec/` | THE WHAT — requirements. Status-free, **protected**. `contracts.md` |
| `docs/` | Planning/ops — `roadmap.md`, `guides/*.md`, `features/*.md` (completed-work archive) |
| `.claude/plans/` | Per-task plans — LOCAL, gitignored, named `{task-id}-{slug}.md` |

Committed: `TODO.md`, `spec/`, `docs/`, `DECISIONS.md`, `CRITICAL.md`.
Gitignored: `.claude/`. Because plans are local, `/implement` archives plan
**substance** into `docs/features/*.md` + `DECISIONS.md` — never a
`Plan: .claude/plans/...` link (dead on other machines).

### Commands
- `/open` — top open tasks (surfaces `- [!]` failed items)
- `/implement {task-id}` — implement, run the verification ladder, archive
- `/new-task` — add a critical issue to Sprint 0
- `/replan` — sprint transition; archive completed work; run the T3 CUJ gate
- `/status` — planning summary + live progress

### Checkbox states
`- [ ]` open · `- [x]` done · `- [!]` attempted-but-failed (awaiting follow-up).

### Task ID format
`S{sprint}-{feature}-{seq}` with a LOWERCASE feature slug — e.g. `S4-be-1`,
`S4-test-1`. Sprint 0 is the always-active triage lane. `TODO.md` carries a
counter comment `<!-- Counters: ... -->` for per-feature seq.

### Verification ladder
- **T1 (every /implement — gates the commit):** `just test` + build + lint/type-check
  (Ruff via `just fmt`), not just the unit runner.
- **T2 (every /implement — gates flipping `- [x]`):** exercise the artifact against the
  task's acceptance criteria. Files existing / build green is NOT done.
- **T3 (sprint-close via /replan):** CUJ simulation against `spec/cujs.md` (when defined).

### CRITICAL escalation
Any task touching a path in `CRITICAL.md` auto-escalates: stronger model + mandatory
review before commit. `spec/` is protected — agents PROPOSE spec changes, never edit
during `/implement`.

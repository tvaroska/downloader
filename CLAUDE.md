# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

REST API Downloader — a high-performance FastAPI web service for programmatic URL content downloading with intelligent article extraction.

- **Version:** 0.5.0
- **Stack:** Python 3.10+, FastAPI, httpx, Redis, Playwright
- **Registry:** `ghcr.io/tvaroska/downloader`
- **Production domain:** `download.tvaroska.sk`

## Development Commands

```bash
# Install dependencies (uses uv package manager)
just install          # uv sync + pre-commit hooks

# Run development server (requires Redis)
just dev              # Uvicorn with hot-reload on port 8000

# Format and lint
just fmt              # ruff check --fix

# Run tests
just test             # pytest tests/ -v
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

## Planning Workflow

This project uses a feature-based planning system:

### File Structure
- **PLAN.md**: Current sprint only (Sprint 0 + active sprint)
- **docs/roadmap.md**: Strategic priorities by feature area
- **docs/features/*.md**: Complete feature history (done, in-progress, planned)

### Commands
- `/open` - Show top 3 open tasks (Sprint 0 first, then current sprint)
- `/implement {task-id}` - Execute a specific task
- `/new-task` - Add critical issue to Sprint 0
- `/replan` - Archive completed work to feature files, update plans

### Task ID Format
- Sprint 0: `S0-{CATEGORY}-{NUMBER}`
- Feature sprints: `S{N}-{CATEGORY}-{NUMBER}`
- Categories: DB, BE, FE, TEST, QA, SEC, INFRA, REL, PERF

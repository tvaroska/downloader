# REST API Downloader

FastAPI web service for programmatic URL content downloading, DOM article extraction (`BeautifulSoup` + `markdownify`), image OCR (`pytesseract` + `Pillow`), headless Chromium PDF/screenshot/SPA rendering with declarative actions (`Playwright`), SSRF-safe webhooks, and stateless-first ethical crawling (`robots.txt` + `Crawl-delay`).

## Security & Resource Isolation (`v0.7.0`)

- **4-Stage SSRF Defense-in-Depth (`SSRFSafeHTTPTransport`)**:
  - Resolves hostnames via non-blocking `asyncio.to_thread` and pins the verified IP address directly at socket connect time (`connect_tcp` in `SSRFSafeNetworkBackend` with `trust_env=False`), closing DNS-rebinding TOCTOU windows.
  - Enforces private/reserved IP blocks across initial DNS pre-flight, every `3xx` HTTP redirect hop, `/robots.txt` fetches, outbound webhooks (`follow_redirects=False`), `/schedules` and `/batch` creation, and Playwright subresource routes (`context.route("**/*", ...)`).
  - Blocks `127.0.0.1`, `*.localhost`, RFC1918 private ranges, link-local / cloud metadata (`169.254.169.254`), `file://` URIs, and hex/octal/integer/IPv4-mapped-IPv6 encodings.
- **Single-Fetch Bounded Chromium Pool (`pdf_semaphore`) & Allowlisted Actions**:
  - Unifies all headless Chromium pathways (`Accept: application/pdf`, `Accept: image/png|jpeg`, `?output=screenshot|pdf`, `?render=true`, `?wait_for=`, and `POST /render`) under a single `pdf_semaphore` (`PDF_CONCURRENCY`, default `CPUx2`, max `12`) without redundant pre-flight HTTP fetches.
  - Uses single-pass `domcontentloaded` navigation with a bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`), eliminating `10s-20s` hangs on tracker-heavy origins (`v0.6.2`).
  - Supports strictly allowlisted declarative Playwright actions (`click`, `fill`, `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; max `20` steps) with zero arbitrary JavaScript evaluation (`src/downloader/browser/actions.py`).
  - Fails fast with `HTTP 503` (`Retry-After: 5`) when the browser pool is saturated on explicit PDF/screenshot/`?render=true` requests, while degrading gracefully to static HTML extraction on automatic JS-detection fallbacks.
  - Automatically recycles Chromium instances after `PDF_MAX_USES_PER_BROWSER=50` renders and scales the pool to zero after `PDF_IDLE_TIMEOUT=600s` idle.
- **Stateless-First Ethical Crawling (`robots.txt` & `Crawl-delay`)**:
  - Evaluates origin `/robots.txt` (`robots:v2:{scheme}:{netloc}`) before `GET /{url}`, `POST /render`, `POST /batch`, and scheduled executions using configurable `USER_AGENT` (`REST-API-Downloader/1.0` default) when `RESPECT_ROBOTS_TXT=true`.
  - Uses a bounded in-memory TTL/LRU cache (`ROBOTS_CACHE_TTL=3600`, `ROBOTS_CACHE_MAX_SIZE=1000`) and monotonic per-domain delay slots out of the box without Redis, upgrading automatically to shared Redis cache keys and atomic Lua `DomainThrottle` reservations when `REDIS_URI` is configured.
  - Honors origin `Crawl-delay` directives capped at `MAX_CRAWL_DELAY` (`30.0s` default).
  - Returns `HTTP 403` (`error_type: "robots_disallowed"`) when disallowed by policy, and intentionally **fails open** with a structured warning log and cached sentinel when `/robots.txt` is unreachable, times out (`ROBOTS_FETCH_TIMEOUT=5.0s`), or is malformed so origin `robots.txt` outages do not block legitimate fetches.
- **Streaming Download Size Guard & OCR Pixel Cap**:
  - Enforces `CONTENT_MAX_DOWNLOAD_SIZE` (`50MB` default) via `Content-Length` pre-checks and streaming byte counters, aborting oversized transfers immediately with `HTTP 413` (`error_type: "content_too_large"`).
  - Caps decoded OCR image dimensions via `OCR_MAX_PIXELS` (`25_000_000` default) against decompression bombs, validates language codes (`eng`, `eng+deu`), and degrades explicitly to `HTTP 503` (`ocr_unavailable`) when `tesseract` is not installed.
- **SSRF-Safe HMAC Webhooks**:
  - Batch and schedule completion webhooks (`webhook_url`, optional `webhook_secret`) validate target URLs at creation and delivery time, sign JSON payloads with `X-Downloader-Signature-256: sha256=<hex>`, never echo `webhook_secret` in API responses, and fire strictly after batch results persist in Redis.
- **Graceful Shutdown, Split Workers & Opt-In OpenTelemetry**:
  - `DrainCoordinator` and `DrainMiddleware` return `HTTP 503` (`service_draining`, `Connection: close`, `Retry-After: 5`) and flip `GET /health` to `503` during shutdown while waiting up to `SHUTDOWN_DRAIN_TIMEOUT` (`30s`) for in-flight requests and active jobs to finish.
  - `DEPLOYMENT_MODE=all|api|worker` keeps stateless single-pod behavior by default (`all`), or separates stateless API pods from Playwright/scheduler workers via Redis `BLMOVE` queues and remote browser RPC (`docker-compose.split.yml`).
  - Opt-in OpenTelemetry (`OTEL_ENABLED=false` default) traces FastAPI, `httpx`, Redis, Playwright, OCR, webhooks, and worker jobs with `trace_id`/`span_id` JSON log correlation, `X-Trace-ID` headers, and W3C `traceparent` queue propagation.
- **Active Moving-Window Rate Limiting**: Multi-tier limits (fallback order: `RATELIMIT_STORAGE_URI`, then `REDIS_URI`, then `memory://`) with right-to-left trusted-proxy `X-Forwarded-For` parsing (`RATELIMIT_TRUSTED_PROXIES`), separate authenticated vs. unauthenticated buckets, `X-RateLimit-{Limit,Remaining,Reset}` headers, and `HTTP 429` (`rate_limit_exceeded`) with `Retry-After`:
  - Download endpoints (`/{url}`, `/render`): 60 requests/minute
  - Batch endpoints (`/batch`, `/jobs/*`): 20 requests/minute
  - Status endpoints (`/health`, `/metrics*`): 200 requests/minute
  - Default endpoints: 100 requests/minute
- **Authentication & Production Surface Hardening**:
  - Constant-time API key verification (`secrets.compare_digest` via `Authorization: Bearer <key>` or `X-API-Key: <key>`) across download, render, batch, schedule, and `/metrics*` endpoints when `DOWNLOADER_KEY` is configured (`GET /health` remains unauthenticated for liveness probes).
  - Leaving `DOWNLOADER_KEY` unset is intended for local development only; in production (`ENVIRONMENT=production`), always set `DOWNLOADER_KEY` (interactive `/docs`, `/redoc`, and `/openapi.json` schemas are automatically disabled with `HTTP 404` when `ENVIRONMENT=production`).
- **Non-Root Container Runtime**: Listens on unprivileged TCP port `:8000` with isolated Playwright browser contexts, `tesseract-ocr`, graceful drain (`--timeout-graceful-shutdown 35`), and structured JSON access logging (`method`, `path`, `status_code`, optional `trace_id`/`span_id`).

## Getting Started

### Minimal Setup (Stateless: No Redis, No Playwright)

Runs out of the box without external dependencies for single-URL downloading, DOM extraction (`text/plain`, `text/markdown`, `text/html`, `application/json`), in-memory rate limiting, and `robots.txt` caching:

```bash
uv sync
uv run python run.py

# Plain text article extraction (default)
curl http://localhost:8000/https://example.com

# Markdown extraction
curl -H "Accept: text/markdown" http://localhost:8000/https://example.com

# Multi-format JSON map in a single fetch
curl -H "Accept: text/html, text/markdown" http://localhost:8000/https://example.com

# Production mode with API key protection
DOWNLOADER_KEY=your-secret-key ENVIRONMENT=production uv run python run.py
curl -H "Authorization: Bearer your-secret-key" -H "Accept: text/plain" \
  http://localhost:8000/https://example.com
```

### Full Setup (Redis + Playwright)

Adds headless Chromium PDF/SPA rendering (`Accept: application/pdf`, `?render=true`), background `/batch` jobs, and cron `/schedules`:

```bash
uv sync
uv run playwright install chromium
docker run -d -p 6379:6379 redis:alpine

REDIS_URI=redis://localhost:6379 DOWNLOADER_KEY=your-secret-key uv run python run.py

# Render PDF via Playwright
curl -H "Authorization: Bearer your-secret-key" -H "Accept: application/pdf" \
  -o page.pdf http://localhost:8000/https://example.com
```

### Docker

```bash
docker run -e DOWNLOADER_KEY=your-secret-key \
  -e REDIS_URI=redis://host.docker.internal:6379 \
  -p 8000:8000 us-central1-docker.pkg.dev/sites-470716/containers/downloader:latest
```

## API Endpoints

See [`spec/contracts.md`](spec/contracts.md) for the complete wire specification.

### URL Download & Content Negotiation (`GET /{url}`)

| `Accept` Header | Response Type | Description |
| :--- | :--- | :--- |
| `text/plain` *(or omitted)* | Plain Text | Extracted `<article>` / `<main>` body text via `BeautifulSoup` (or OCR text for image origins) |
| `text/markdown` | Markdown | Structured Markdown converted via `markdownify` (or OCR Markdown for image origins) |
| `text/html` | HTML | Raw HTML document (or rendered DOM when `?render=true` / `?actions=`) |
| `application/pdf` | PDF | Headless Chromium PDF render via `Playwright` |
| `image/png`, `image/jpeg` | Screenshot | Headless Chromium PNG/JPEG capture via `Playwright` |
| `application/json` | JSON | Base64 content payload with response metadata |
| Multiple MIME types (e.g. `text/html, text/markdown`) | JSON Map | Keyed JSON object (`{"text/html": "...", "text/markdown": "..."}`) |

Query parameters on `GET /{url}`:
- `?render=true`: Force headless Chromium rendering before extraction.
- `?wait_for=<css_selector>`: Wait for a CSS selector in Playwright before capturing DOM/PDF/screenshot.
- `?output=screenshot|pdf`: Explicitly toggle browser screenshot or PDF output (`?full_page=true`, `?screenshot_format=png|jpeg`).
- `?actions=<json>`: JSON array of allowlisted Playwright interactions (`click`, `fill`, `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; max `20`).
- `?ocr=true` / `?ocr_lang=eng`: Require Tesseract OCR text extraction on image responses (`HTTP 503` `ocr_unavailable` if `tesseract` is missing).

### Render, Batch, Schedules, Health & Metrics Endpoints

| Method & Path | Requirement | Description |
| :--- | :--- | :--- |
| `GET /health` | Stateless | Liveness, version, deployment mode, worker stats (`503` while draining) |
| `POST /render` | Stateless (or Worker RPC) | Scripted headless browser capture with allowlisted `actions` (`text`, `markdown`, `html`, `pdf`, `screenshot`) |
| `POST /batch` | `REDIS_URI` | Submit asynchronous multi-URL download batch with optional `actions` and `webhook_url` (`<= 50` URLs default) |
| `GET /jobs/{job_id}/status` | `REDIS_URI` | Poll batch job status and completion percentage |
| `GET /jobs/{job_id}/results` | `REDIS_URI` | Retrieve completed per-URL batch results as JSON |
| `DELETE /jobs/{job_id}` | `REDIS_URI` | Cancel a pending or running batch job |
| `POST /schedules` | `REDIS_URI` | Create a cron-scheduled recurring download (`text`, `markdown`, `html`, `pdf`, `screenshot`, `json`, `raw`) with optional `webhook_url` |
| `GET /schedules` | `REDIS_URI` | List configured schedules and next run timestamps |
| `GET /schedules/{schedule_id}` | `REDIS_URI` | Inspect a single schedule configuration |
| `GET /schedules/{schedule_id}/history` | `REDIS_URI` | Paginated execution history (`?limit=20&offset=0`, max `100`) |
| `DELETE /schedules/{schedule_id}` | `REDIS_URI` | Delete a scheduled download |
| `GET /metrics` | Auth (if key set) | Prometheus text exposition metrics (`robots_*`, request latencies, errors) |
| `GET /metrics/performance` | Auth (if key set) | JSON uptime, request counts, error rates, and avg/min/max/p95 latencies |
| `GET /metrics/health-score` | Auth (if key set) | Heuristic operational health score (`0-100`) derived from error rate, latency, and pool saturation |
| `GET /metrics/live` | Auth (if key set) | Real-time `pdf_semaphore` / batch concurrency and HTTP connection pool stats |

## Client SDKs

First-party typed SDKs with offline unit tests live in [`sdks/`](sdks):
- Python (`httpx` + Pydantic v2, sync & async): [`sdks/python/README.md`](sdks/python/README.md)
- JavaScript / TypeScript (built-in `fetch`, zero runtime deps): [`sdks/javascript/README.md`](sdks/javascript/README.md)
- Go (`net/http` stdlib, zero external deps): [`sdks/go`](sdks/go)

## Configuration

Configured via environment variables (see `.env.example` and [`docs/guides/configuration.md`](docs/guides/configuration.md) for the full reference):

### Core & Authentication
- `DOWNLOADER_KEY`: API key for Bearer / `X-API-Key` authentication (optional in local dev; required in production)
- `REDIS_URI`: Redis connection string for `/batch`, `/schedules`, split worker queues, and shared rate-limit / `robots.txt` state
- `ENVIRONMENT`: `development`, `staging`, or `production` (`production` disables `/docs`, `/redoc`, and `/openapi.json`)
- `DEPLOYMENT_MODE`: `all` (default monolith/stateless pod), `api` (offloads batch/browser to Redis), or `worker` (standalone `src.downloader.worker`)

### HTTP Client, OCR & Content Limits
- `HTTP_MAX_CONNECTIONS`: Max total HTTP connections (default: `200`)
- `HTTP_REQUEST_TIMEOUT`: Outbound request timeout in seconds (default: `30`)
- `CONTENT_MAX_DOWNLOAD_SIZE`: Streaming byte cap in bytes (default: `52428800` / `50MB`; returns `HTTP 413` `content_too_large`)
- `OCR_ENABLED` / `OCR_DEFAULT_LANGUAGE` / `OCR_MAX_PIXELS`: Tesseract OCR extraction (`true`, `eng`, `25_000_000` pixel cap)

### Webhooks, Shutdown Drain, Workers & OpenTelemetry
- `WEBHOOK_ENABLED` / `WEBHOOK_TIMEOUT_SECONDS` / `WEBHOOK_MAX_RETRIES`: Post-persistence SSRF-safe HMAC-SHA256 webhooks (`true`, `10.0s`, `3`)
- `SHUTDOWN_DRAIN_TIMEOUT` / `SHUTDOWN_POLL_INTERVAL`: Graceful shutdown connection and job drain (`30.0s`, `0.25s`)
- `WORKER_POLL_TIMEOUT` / `WORKER_BROWSER_TASK_TIMEOUT` / `WORKER_MAX_CONCURRENT_JOBS`: Split worker queue polling and browser RPC settings (`5.0s`, `60.0s`, `4`)
- `OTEL_ENABLED` / `OTEL_SERVICE_NAME` / `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_CONSOLE_EXPORTER`: Opt-in OpenTelemetry distributed tracing (`false` default)

### Ethical Crawling
- `USER_AGENT`: Outbound HTTP and Playwright `User-Agent` (default: `REST-API-Downloader/1.0`)
- `RESPECT_ROBOTS_TXT`: Enforce `robots.txt` and `Crawl-delay` (default: `true`; returns `HTTP 403` `robots_disallowed`)
- `ROBOTS_CACHE_TTL`: `robots.txt` cache TTL in seconds (default: `3600`)
- `ROBOTS_CACHE_MAX_SIZE`: Bounded in-memory LRU cache capacity (default: `1000`)
- `ROBOTS_FETCH_TIMEOUT`: Timeout in seconds for `/robots.txt` fetches (default: `5.0`)
- `MAX_CRAWL_DELAY`: Cap on honored `Crawl-delay` in seconds (default: `30.0`)

### Rate Limiting
- `RATELIMIT_ENABLED`: Enable moving-window rate limiting (default: `true`)
- `RATELIMIT_TRUSTED_PROXIES`: Trusted proxy CIDRs for right-to-left `X-Forwarded-For` resolution
- `RATELIMIT_DEFAULT_LIMIT`: Default limit (default: `"100/minute"`)
- `RATELIMIT_DOWNLOAD_LIMIT`: Download and `/render` endpoint limit (default: `"60/minute"`)
- `RATELIMIT_BATCH_LIMIT`: Batch job limit (default: `"20/minute"`)
- `RATELIMIT_STATUS_LIMIT`: Status/metrics endpoint limit (default: `"200/minute"`)

### Batch & Browser Pool Lifecycle
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: `CPUx8`, max `50`)
- `BATCH_MAX_URLS_PER_BATCH`: Max URLs per batch (default: `50`)
- `PDF_CONCURRENCY`: Max concurrent Playwright renders across PDF, screenshot, and HTML (default: `CPUx2`, max `12`)
- `PDF_PAGE_LOAD_TIMEOUT`: Playwright timeout in ms (default: `30000`)
- `PDF_MAX_USES_PER_BROWSER`: Recycle Chromium instance after N renders (default: `50`)
- `PDF_IDLE_TIMEOUT`: Close idle browser pool after N seconds / scale-to-zero (default: `600.0`)

### Logging & Network Security
- `LOG_JSON_LOGS`: Emit structured JSON logs with top-level `method`, `path`, `status_code`, and optional `trace_id`/`span_id` on `uvicorn.access` (default: `false`; set `true` in production)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private/reserved IPs across DNS-pinned sockets, redirects, webhooks, `robots.txt`, and Playwright requests (default: `true`)
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: `http://localhost:3000,http://localhost:8080` for localhost only; set explicitly in production)

## Testing

The repository uses a 4-marker pytest suite (`smoke`, `unit`, `integration`, `e2e`) with `885+` test cases (`752+` offline) plus standalone test suites for each SDK:

```bash
# Quick smoke tests (<3s)
uv run --no-sync pytest -m smoke -v

# Fast offline gate (smoke + unit)
uv run --no-sync pytest -m "not (integration or e2e or network or requires_redis or requires_playwright)"

# SDK test suites (Python, JavaScript/TypeScript, Go)
uv run --no-sync pytest sdks/python/tests -c sdks/python/pyproject.toml -v
node --test sdks/javascript/test/client.test.mjs
go -C sdks/go vet ./... && go -C sdks/go test -v ./...

# Integration tests (Playwright + component verification)
uv run --no-sync pytest -m integration -v

# Full E2E tests (Docker + Redis)
uv run --no-sync pytest -m e2e -v
```

## Implementation Status (`v0.7.0`)

**Delivered through `v0.7.0`:**
- Single-pass `domcontentloaded` + bounded `networkidle` settle (`v0.6.2`) eliminating navigation hangs on tracker-heavy origins
- Direct URL endpoint (`GET /{url}`) with single- and multi-format `Accept` content negotiation (`text/plain`, `text/markdown`, `text/html`, `application/json`, `application/pdf`, `image/png`, `image/jpeg`) plus `?output=screenshot|pdf`, `?actions=`, and `?ocr=true`
- Scripted rendering endpoint (`POST /render`) executing strictly allowlisted declarative Playwright actions (`src/downloader/browser/actions.py`, zero raw JS eval)
- SSRF-safe HMAC-SHA256 webhook delivery (`src/downloader/services/webhooks.py`) firing post-persistence on `/batch` and `/schedules`
- Image-to-text OCR (`src/downloader/transformers/ocr.py` via `pytesseract` + `Pillow` + `tesseract-ocr`) with decompression-bomb pixel caps and `HTTP 503` graceful degradation
- Typed client SDKs in [`sdks/python/README.md`](sdks/python/README.md), [`sdks/javascript/README.md`](sdks/javascript/README.md), and [`sdks/go`](sdks/go)
- Graceful shutdown connection and job drain (`src/downloader/drain.py`, `HTTP 503 service_draining` + `Connection: close`)
- Split API + Worker deployment mode (`DEPLOYMENT_MODE=all|api|worker`, `src/downloader/worker.py`, `src/downloader/worker_queue.py`, `docker-compose.split.yml`)
- Opt-in OpenTelemetry distributed tracing (`src/downloader/telemetry.py`, `OTEL_ENABLED=false` default, W3C `traceparent` across Redis, `X-Trace-ID`, and JSON log correlation)
- DNS-pinned `SSRFSafeHTTPTransport` (`trust_env=False`, `connect_tcp` IP pinning), `3xx` redirect re-validation, Playwright `context.route("**/*", ...)` SSRF guards, stateless-first `robots.txt` + `Crawl-delay`, moving-window rate limiting, and production `/metrics*` auth + OpenAPI disablement

**Deferred Backlog (See [`docs/roadmap.md`](docs/roadmap.md) and [`DECISIONS.md`](DECISIONS.md)):**
- **Post-`v0.7.0` (`v0.8.0+`, Deferred until multi-tenant deployment):** Per-API-key `QuotaConfig`, `GET /usage`, tenant bandwidth accounting, and tier-based plans

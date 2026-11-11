# REST API Downloader

FastAPI web service for programmatic URL content downloading, DOM article extraction (`BeautifulSoup` + `markdownify`), headless Chromium PDF/SPA rendering (`Playwright`), and stateless-first ethical crawling (`robots.txt` + `Crawl-delay`).

## Security & Resource Isolation (`v0.6.1`)

- **4-Stage SSRF Defense-in-Depth (`SSRFSafeHTTPTransport`)**:
  - Resolves hostnames via non-blocking `asyncio.to_thread` and pins the verified IP address directly at socket connect time (`connect_tcp` in `SSRFSafeNetworkBackend` with `trust_env=False`), closing DNS-rebinding TOCTOU windows.
  - Enforces private/reserved IP blocks across initial DNS pre-flight, every `3xx` HTTP redirect hop, `/robots.txt` fetches, `/schedules` and `/batch` creation, and Playwright subresource routes (`context.route("**/*", ...)`).
  - Blocks `127.0.0.1`, `*.localhost`, RFC1918 private ranges, link-local / cloud metadata (`169.254.169.254`), `file://` URIs, and hex/octal/integer/IPv4-mapped-IPv6 encodings.
- **Single-Fetch Bounded Chromium Pool (`pdf_semaphore`)**:
  - Unifies all headless Chromium pathways (`Accept: application/pdf`, `?render=true`, and `?wait_for=`) under a single `pdf_semaphore` (`PDF_CONCURRENCY`, default `CPUx2`, max `12`) without redundant pre-flight HTTP fetches.
  - Fails fast with `HTTP 503` (`Retry-After: 5`) when the browser pool is saturated on explicit PDF or `?render=true` requests, while degrading gracefully to static HTML extraction on automatic JS-detection fallbacks.
  - Automatically recycles Chromium instances after `PDF_MAX_USES_PER_BROWSER=50` renders and scales the pool to zero after `PDF_IDLE_TIMEOUT=600s` idle.
- **Stateless-First Ethical Crawling (`robots.txt` & `Crawl-delay`)**:
  - Evaluates origin `/robots.txt` (`robots:v2:{scheme}:{netloc}`) before `GET /{url}`, `POST /batch`, and scheduled executions using configurable `USER_AGENT` (`REST-API-Downloader/1.0` default) when `RESPECT_ROBOTS_TXT=true`.
  - Uses a bounded in-memory TTL/LRU cache (`ROBOTS_CACHE_TTL=3600`, `ROBOTS_CACHE_MAX_SIZE=1000`) and monotonic per-domain delay slots out of the box without Redis, upgrading automatically to shared Redis cache keys and atomic Lua `DomainThrottle` reservations when `REDIS_URI` is configured.
  - Honors origin `Crawl-delay` directives capped at `MAX_CRAWL_DELAY` (`30.0s` default).
  - Returns `HTTP 403` (`error_type: "robots_disallowed"`) when disallowed by policy, and intentionally **fails open** with a structured warning log and cached sentinel when `/robots.txt` is unreachable, times out (`ROBOTS_FETCH_TIMEOUT=5.0s`), or is malformed so origin `robots.txt` outages do not block legitimate fetches.
- **Streaming Download Size Guard**: Enforces `CONTENT_MAX_DOWNLOAD_SIZE` (`50MB` default) via `Content-Length` pre-checks and streaming byte counters, aborting oversized transfers immediately with `HTTP 413` (`error_type: "content_too_large"`).
- **Active Moving-Window Rate Limiting**: Multi-tier limits (fallback order: `RATELIMIT_STORAGE_URI`, then `REDIS_URI`, then `memory://`) with right-to-left trusted-proxy `X-Forwarded-For` parsing (`RATELIMIT_TRUSTED_PROXIES`), separate authenticated vs. unauthenticated buckets, `X-RateLimit-{Limit,Remaining,Reset}` headers, and `HTTP 429` (`rate_limit_exceeded`) with `Retry-After`:
  - Download endpoints (`/{url}`): 60 requests/minute
  - Batch endpoints (`/batch`, `/jobs/*`): 20 requests/minute
  - Status endpoints (`/health`, `/metrics*`): 200 requests/minute
  - Default endpoints: 100 requests/minute
- **Authentication & Production Surface Hardening**:
  - Constant-time API key verification (`secrets.compare_digest` via `Authorization: Bearer <key>` or `X-API-Key: <key>`) across download, batch, schedule, and `/metrics*` endpoints when `DOWNLOADER_KEY` is configured (`GET /health` remains unauthenticated for liveness probes).
  - Leaving `DOWNLOADER_KEY` unset is intended for local development only; in production (`ENVIRONMENT=production`), always set `DOWNLOADER_KEY` (interactive `/docs`, `/redoc`, and `/openapi.json` schemas are automatically disabled with `HTTP 404` when `ENVIRONMENT=production`).
- **Non-Root Container Runtime**: Listens on unprivileged TCP port `:8000` with isolated Playwright browser contexts and structured JSON access logging (`method`, `path`, `status_code`).

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
| `text/plain` *(or omitted)* | Plain Text | Extracted `<article>` / `<main>` body text via `BeautifulSoup` |
| `text/markdown` | Markdown | Structured Markdown converted via `markdownify` |
| `text/html` | HTML | Raw HTML document (or rendered DOM when `?render=true`) |
| `application/pdf` | PDF | Headless Chromium PDF render via `Playwright` |
| `application/json` | JSON | Base64 content payload with response metadata |
| Multiple MIME types (e.g. `text/html, text/markdown`) | JSON Map | Keyed JSON object (`{"text/html": "...", "text/markdown": "..."}`) |

Query parameters on `GET /{url}`:
- `?render=true`: Force headless Chromium rendering before extraction.
- `?wait_for=<css_selector>`: Wait for a CSS selector in Playwright before capturing DOM/PDF.

### Batch, Schedules, Health & Metrics Endpoints

| Method & Path | Requirement | Description |
| :--- | :--- | :--- |
| `GET /health` | Stateless | Liveness, version, and authentication status check |
| `POST /batch` | `REDIS_URI` | Submit asynchronous multi-URL download batch (`<= 50` URLs default) |
| `GET /jobs/{job_id}/status` | `REDIS_URI` | Poll batch job status and completion percentage |
| `GET /jobs/{job_id}/results` | `REDIS_URI` | Retrieve completed per-URL batch results as JSON |
| `DELETE /jobs/{job_id}` | `REDIS_URI` | Cancel a pending or running batch job |
| `POST /schedules` | `REDIS_URI` | Create a cron-scheduled recurring download (`text`, `markdown`, `html`, `pdf`, `json`, `raw`) |
| `GET /schedules` | `REDIS_URI` | List configured schedules and next run timestamps |
| `GET /schedules/{schedule_id}` | `REDIS_URI` | Inspect a single schedule configuration |
| `GET /schedules/{schedule_id}/history` | `REDIS_URI` | Paginated execution history (`?limit=20&offset=0`, max `100`) |
| `DELETE /schedules/{schedule_id}` | `REDIS_URI` | Delete a scheduled download |
| `GET /metrics` | Auth (if key set) | Prometheus text exposition metrics (`robots_*`, request latencies, errors) |
| `GET /metrics/performance` | Auth (if key set) | JSON uptime, request counts, error rates, and avg/min/max/p95 latencies |
| `GET /metrics/health-score` | Auth (if key set) | Heuristic operational health score (`0-100`) derived from error rate, latency, and pool saturation |
| `GET /metrics/live` | Auth (if key set) | Real-time `pdf_semaphore` / batch concurrency and HTTP connection pool stats |

## Configuration

Configured via environment variables (see `.env.example` and [`docs/guides/configuration.md`](docs/guides/configuration.md) for the full reference):

### Core & Authentication
- `DOWNLOADER_KEY`: API key for Bearer / `X-API-Key` authentication (optional in local dev; required in production)
- `REDIS_URI`: Redis connection string for `/batch`, `/schedules`, and shared rate-limit / `robots.txt` state
- `ENVIRONMENT`: `development`, `staging`, or `production` (`production` disables `/docs`, `/redoc`, and `/openapi.json`)

### HTTP Client & Content Limits
- `HTTP_MAX_CONNECTIONS`: Max total HTTP connections (default: `200`)
- `HTTP_REQUEST_TIMEOUT`: Outbound request timeout in seconds (default: `30`)
- `CONTENT_MAX_DOWNLOAD_SIZE`: Streaming byte cap in bytes (default: `52428800` / `50MB`; returns `HTTP 413` `content_too_large`)

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
- `RATELIMIT_DOWNLOAD_LIMIT`: Download endpoint limit (default: `"60/minute"`)
- `RATELIMIT_BATCH_LIMIT`: Batch job limit (default: `"20/minute"`)
- `RATELIMIT_STATUS_LIMIT`: Status/metrics endpoint limit (default: `"200/minute"`)

### Batch & Browser Pool Lifecycle
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: `CPUx8`, max `50`)
- `BATCH_MAX_URLS_PER_BATCH`: Max URLs per batch (default: `50`)
- `PDF_CONCURRENCY`: Max concurrent Playwright renders across PDF and HTML (default: `CPUx2`, max `12`)
- `PDF_PAGE_LOAD_TIMEOUT`: Playwright timeout in ms (default: `30000`)
- `PDF_MAX_USES_PER_BROWSER`: Recycle Chromium instance after N renders (default: `50`)
- `PDF_IDLE_TIMEOUT`: Close idle browser pool after N seconds / scale-to-zero (default: `600.0`)

### Logging & Network Security
- `LOG_JSON_LOGS`: Emit structured JSON logs with top-level `method`, `path`, and `status_code` on `uvicorn.access` (default: `false`; set `true` in production)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private/reserved IPs across DNS-pinned sockets, redirects, `robots.txt`, and Playwright requests (default: `true`)
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: `http://localhost:3000,http://localhost:8080` for localhost only; set explicitly in production)

## Testing

The repository uses a 4-marker pytest suite (`smoke`, `unit`, `integration`, `e2e`) with `752+` test cases:

```bash
# Quick smoke tests (<3s)
uv run --no-sync pytest -m smoke -v

# Fast offline gate (smoke + unit)
uv run --no-sync pytest -m "smoke or unit" -v

# Integration tests (Playwright + component verification)
uv run --no-sync pytest -m integration -v

# Full E2E tests (Docker + Redis)
uv run --no-sync pytest -m e2e -v
```

## Implementation Status (`v0.6.1`)

**Delivered in `v0.6.1`:**
- Direct URL endpoint (`GET /{url}`) with single- and multi-format `Accept` content negotiation (`text/plain`, `text/markdown`, `text/html`, `application/json`, `application/pdf`)
- DNS-pinned `SSRFSafeHTTPTransport` (`trust_env=False`, `connect_tcp` IP pinning), `3xx` redirect re-validation, and Playwright `context.route("**/*", ...)` subresource SSRF guards
- Stateless-first ethical crawling (`robots.txt` `robots:v2:{scheme}:{netloc}` LRU/Redis cache, atomic Lua `Crawl-delay` throttling, `HTTP 403` `robots_disallowed`, Prometheus `robots_*` counters)
- Single-fetch Playwright pipeline under unified `pdf_semaphore` with max-use recycling (`50`) and idle scale-to-zero (`600s`)
- Streaming download size guard (`HTTP 413` `content_too_large`) and moving-window rate limiting (`HTTP 429` + `Retry-After` + `X-RateLimit-*`)
- Constant-time API key authentication across download, batch, schedule, and `/metrics*` endpoints, plus `ENVIRONMENT=production` OpenAPI disablement
- Redis-backed `/batch` processing and cron `/schedules` with execution history

**Deferred Backlog (See [`docs/roadmap.md`](docs/roadmap.md) and [`DECISIONS.md`](DECISIONS.md)):**
- **Sprint 5 (Deferred until multi-tenant deployment):** Per-API-key `QuotaConfig`, `GET /usage`, and tenant bandwidth accounting
- **Sprint 6 Candidates:** Screenshot/PDF query toggle (`?output=screenshot|pdf`), webhook notifications, OCR (image-to-text), and typed client SDKs

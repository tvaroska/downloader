# REST API Downloader

High-performance web service for programmatic URL content downloading with intelligent article extraction.

## 🚀 Features

- **Direct URL Access**: Simple `/{url}` endpoint structure
- **Background Batch Processing**: Asynchronous job-based processing with progress tracking
- **Content Negotiation**: Multiple response formats via Accept headers
- **Stateless-First Ethical Crawling**: Automatic `robots.txt` compliance, configurable `User-Agent`, and per-domain `Crawl-delay` throttling with bounded in-memory TTL/LRU caching (plus optional Redis coordination)
- **PDF & SPA Rendering**: JavaScript-rendered PDFs and DOM extraction using Playwright with automatic browser recycling (`PDF_MAX_USES_PER_BROWSER=50`) and idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`)
- **API Key Protection**: Optional authentication via environment variable
- **Intelligent Extraction**: BeautifulSoup-powered article text extraction
- **Defense-in-Depth Security**: Pre-flight, redirect (`3xx`), and Playwright network-route SSRF protection, streaming `CONTENT_MAX_DOWNLOAD_SIZE` enforcement (`HTTP 413`), and active rate limiting (`HTTP 429` + `Retry-After`)
- **Performance**: Async HTTP client with connection pooling and streaming bounds
- **Scheduled Downloads**: Cron-based recurring downloads with execution history
- **Production Ready**: Non-root Docker container on unprivileged port `:8000` with structured JSON access logging (`method`, `path`, `status_code`)
- **Comprehensive Testing**: Fast offline smoke/unit/hygiene verification plus integration and E2E tiers

## 📋 API Endpoints

### Health Check
```http
GET /health
```
Returns service health status and version.

### URL Download with Content Negotiation
```http
GET /{url}
Accept: {format}
```

**Supported Accept Header Formats:**

| Accept Header | Response Type | Description |
|---------------|---------------|-------------|
| `text/plain` | Plain Text | Extracted article content, clean text only |
| `text/markdown` | Markdown | Structured markdown with headings and links |
| `text/html` | HTML | Original HTML content |
| `application/pdf` | PDF | JavaScript-rendered PDF via Playwright |
| `application/json` | JSON | Base64 content with metadata |
| *No Accept header* | **Plain Text (Default)** | **Default format returns extracted article text** |

> **Single- vs Multi-Format `Accept` Negotiation:** Requesting a single MIME type returns raw format bytes directly (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, or the `application/json` envelope). Requesting multiple MIME types (for example `Accept: text/html, text/markdown`) returns a JSON object keyed by MIME type (`{"text/html": "...", "text/markdown": "..."}`).

### Background Batch Processing

Submit jobs for processing multiple URLs asynchronously with progress tracking.

#### Submit Batch Job
```http
POST /batch
Content-Type: application/json

{
  "urls": [
    {"url": "https://example.com", "format": "text"},
    {"url": "https://github.com", "format": "markdown"}
  ],
  "default_format": "text",
  "concurrency_limit": 10,
  "timeout_per_url": 30
}
```

Returns job ID for tracking progress and retrieving results.

#### Check Job Status
```http
GET /jobs/{job_id}/status
```

Returns current job status, progress percentage, and processing statistics.

#### Download Results
```http
GET /jobs/{job_id}/results
```

Downloads complete job results as JSON when processing is finished.

#### Cancel Job
```http
DELETE /jobs/{job_id}
```

Cancels a running or pending job.

### Scheduled Downloads

Create cron-based schedules for recurring URL downloads. Requires Redis.

#### Create Schedule
```http
POST /schedules
Content-Type: application/json

{
  "name": "Daily news digest",
  "url": "https://example.com/news",
  "cron_expression": "0 9 * * *",
  "format": "markdown"
}
```

Creates a scheduled download. Supported formats: `text`, `markdown`, `html`, `pdf`, `json`, `raw`.

#### List Schedules
```http
GET /schedules
```

Returns all configured schedules with next run times.

#### Get Schedule Details
```http
GET /schedules/{schedule_id}
```

Returns schedule configuration and next run time.

#### Get Execution History
```http
GET /schedules/{schedule_id}/history?limit=20&offset=0
```

Returns paginated execution history with status, duration, and error details. Default limit: 20 (max 100).

#### Delete Schedule
```http
DELETE /schedules/{schedule_id}
```

Removes a scheduled download.

### Metrics

Monitor service performance and health.

#### Prometheus Metrics
```http
GET /metrics
```
Returns all metrics in Prometheus text exposition format.

#### Performance Summary
```http
GET /metrics/performance
```
Returns JSON with uptime, request counts, error rates, and per-endpoint response time statistics (avg, min, max, p95).

#### Health Score
```http
GET /metrics/health-score
```
Returns a health score (0-100) with status (`healthy`, `degraded`, `unhealthy`) and contributing factors.

#### Live System Metrics
```http
GET /metrics/live
```
Returns real-time system metrics including concurrency usage (PDF/batch semaphores), connection pool stats, and performance data.

## 🔐 Authentication

API key authentication is **optional** and controlled by the `DOWNLOADER_KEY` environment variable.

### Without Authentication (Default)
If `DOWNLOADER_KEY` is not set, all endpoints are publicly accessible:

```bash
curl -H "Accept: text/plain" http://localhost:8000/https://example.com
```

### With Authentication
Set the `DOWNLOADER_KEY` environment variable to enable API key protection:

```bash
# Start server with authentication
DOWNLOADER_KEY=your-secret-key uv run python run.py
```

Once enabled, provide the API key using any of these methods:

**1. Bearer Token (Recommended)**
```bash
curl -H "Authorization: Bearer your-secret-key" \
     -H "Accept: text/plain" \
     http://localhost:8000/https://example.com
```

**2. X-API-Key Header**
```bash
curl -H "X-API-Key: your-secret-key" \
     -H "Accept: text/plain" \
     http://localhost:8000/https://example.com
```


### Authentication Status
Check if authentication is enabled via the health endpoint:

```bash
curl http://localhost:8000/health
```

Returns authentication status and supported methods when enabled.

## 🔥 Getting Started

### Minimal Setup (no Redis, no Playwright)

Works out of the box — download URLs and extract content in text, markdown, HTML, or JSON formats.

```bash
# Install dependencies
uv sync

# Start the server
uv run python run.py

# Download a page as plain text
curl http://localhost:8000/https://example.com

# Get markdown format
curl -H "Accept: text/markdown" http://localhost:8000/https://example.com
```

**Available features:** single URL download (`GET /{url}`), content negotiation (text/markdown/HTML/JSON), health check, metrics.

**Not available without Redis:** batch processing, scheduled downloads.
**Not available without Playwright:** PDF generation.

### Full Setup (Redis + Playwright)

Unlocks batch processing, scheduled downloads, and PDF output.

```bash
# Install dependencies
uv sync

# Install Playwright browser for PDF generation
uv run playwright install chromium

# Start Redis (e.g., via Docker)
docker run -d -p 6379:6379 redis:alpine

# Start the server with Redis
REDIS_URI=redis://localhost:6379 uv run python run.py

# Generate a PDF
curl -H "Accept: application/pdf" -o page.pdf \
  http://localhost:8000/https://example.com

# Submit a batch job
curl -X POST http://localhost:8000/batch \
  -H "Content-Type: application/json" \
  -d '{"urls": [{"url": "https://example.com"}, {"url": "https://github.com"}]}'
```

### Using Docker

```bash
# Run without authentication (container listens on unprivileged port 8000)
docker run -p 8000:8000 us-central1-docker.pkg.dev/sites-470716/containers/downloader:latest

# Run with API key and Redis
docker run -e DOWNLOADER_KEY=your-secret-key \
  -e REDIS_URI=redis://host.docker.internal:6379 \
  -p 8000:8000 us-central1-docker.pkg.dev/sites-470716/containers/downloader:latest
```

## 📖 Usage Examples

### Extract Article Text
```bash
curl -H "Accept: text/plain" \
  "http://localhost:8000/https://news.ycombinator.com"
```

### Get Markdown Format
```bash
curl -H "Accept: text/markdown" \
  "http://localhost:8000/https://github.com/python/cpython"
```

### Get JSON with Metadata
```bash
curl -H "Accept: application/json" \
  "http://localhost:8000/https://httpbin.org/json" | jq
```

### Get Original HTML
```bash
curl -H "Accept: text/html" \
  "http://localhost:8000/https://example.com"
```

## 🛡️ Security & Ethical Crawling

- **Defense-in-Depth SSRF Protection**: Blocks localhost (`127.0.0.1`), RFC1918 private IPs, `file://` URLs, and cloud metadata (`169.254.169.254`) across initial DNS pre-flight, every `3xx` HTTP redirect hop, `robots.txt` fetches, and Playwright subresource routes (`context.route("**/*", ...)`).
- **Stateless-First Ethical Crawling (`robots.txt` & `Crawl-delay`)**:
  - Evaluates origin `/robots.txt` before `GET /{url}` and `POST /batch` using configurable `USER_AGENT` (`REST-API-Downloader/1.0` default) when `RESPECT_ROBOTS_TXT=true`.
  - Uses a bounded in-memory TTL/LRU cache (`ROBOTS_CACHE_TTL=3600`, `ROBOTS_CACHE_MAX_SIZE=1000`) and per-domain monotonic delay slots out-of-the-box without Redis, upgrading automatically to shared Redis keys when `REDIS_URI` is set.
  - Honors origin `Crawl-delay` directives capped at `MAX_CRAWL_DELAY` (`30.0s` default).
  - Returns `HTTP 403` (`error_type: "robots_disallowed"`) when disallowed, and **fails open** with a structured warning log + cached sentinel when `/robots.txt` is unreachable, times out (`ROBOTS_FETCH_TIMEOUT=5.0s`), or is malformed.
- **Streaming Download Size Guard**: Enforces `CONTENT_MAX_DOWNLOAD_SIZE` (`50MB` default) via `Content-Length` pre-checks and streaming byte counters, aborting oversized transfers immediately with `HTTP 413` (`error_type: "content_too_large"`).
- **Active Moving-Window Rate Limiting**: Multi-tier limits (`RATELIMIT_STORAGE_URI → REDIS_URI → memory://`) emitting `X-RateLimit-{Limit,Remaining,Reset}` headers and `HTTP 429` (`rate_limit_exceeded`) with `Retry-After`:
  - Download endpoints (`/{url}`): 60 requests/minute
  - Batch endpoints (`/batch`, `/jobs/*`): 20 requests/minute
  - Status endpoints (`/health`, `/metrics*`): 200 requests/minute
  - Default endpoints: 100 requests/minute
- **API Key Authentication**: Optional Bearer token or `X-API-Key` header
- **Non-Root Unprivileged Container**: Listens on TCP `:8000` with isolated Playwright browser contexts, automatic browser recycling (`PDF_MAX_USES_PER_BROWSER=50`), and idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`)

## 🧪 Testing

The project uses a 4-marker pytest strategy (`smoke`, `unit`, `integration`, `e2e`) for fast verification:

```bash
# Quick smoke tests (<3s) - for rapid feedback
uv run --no-sync pytest -m smoke -v

# Fast offline gate (smoke + unit)
uv run --no-sync pytest -m "smoke or unit" -v

# Integration tests (~15s) - for component & Playwright testing
uv run --no-sync pytest -m integration -v

# Full E2E tests (~60s) - for comprehensive Docker + Redis validation
uv run --no-sync pytest -m e2e -v

# Run all tests with coverage
uv run --no-sync pytest --cov=src --cov-report=html

# Test specific functionality
uv run --no-sync pytest tests/api/test_download.py -v
```

**Test Statistics**: 764+ tests across `smoke`, `unit`, `integration`, and `e2e` tiers.

## 🏗️ Architecture

- **FastAPI**: Modern async web framework
- **httpx**: High-performance HTTP client
- **BeautifulSoup**: Intelligent HTML parsing and article extraction
- **Pydantic**: Data validation and serialization
- **Docker**: Production-ready containerization

## 📈 Performance

- **Async Support**: Non-blocking I/O operations
- **Connection Pooling**: Efficient HTTP connection reuse
- **Smart Extraction**: Targets main content areas (`<article>`, `<main>`, etc.)
- **Memory Efficient**: Streaming support for large files

## 🔧 Configuration

The service is configured via environment variables with sensible defaults:

### Core Settings
- `DOWNLOADER_KEY`: API key for authentication (optional)
- `REDIS_URI`: Redis connection string for job management and rate limiting
- `ENVIRONMENT`: Runtime environment (development/staging/production)

### HTTP Client & Content Limits
- `HTTP_MAX_CONNECTIONS`: Max total connections (default: 200)
- `HTTP_REQUEST_TIMEOUT`: Request timeout in seconds (default: 30)
- `CONTENT_MAX_DOWNLOAD_SIZE`: Streaming byte cap in bytes (default: 52428800 / 50MB; returns `HTTP 413` `content_too_large`)

### Ethical Crawling
- `USER_AGENT`: Outbound HTTP & Playwright `User-Agent` (default: `REST-API-Downloader/1.0`)
- `RESPECT_ROBOTS_TXT`: Enforce `robots.txt` & `Crawl-delay` (default: `true`; returns `HTTP 403` `robots_disallowed`)
- `ROBOTS_CACHE_TTL`: `robots.txt` cache TTL in seconds (default: `3600`)
- `ROBOTS_CACHE_MAX_SIZE`: Bounded in-memory LRU cache capacity (default: `1000`)
- `ROBOTS_FETCH_TIMEOUT`: Timeout in seconds for `/robots.txt` fetches (default: `5.0`)
- `MAX_CRAWL_DELAY`: Cap on honored `Crawl-delay` in seconds (default: `30.0`)

### Rate Limiting
- `RATELIMIT_ENABLED`: Enable moving-window rate limiting (default: `true`)
- `RATELIMIT_DEFAULT_LIMIT`: Default limit (default: `"100/minute"`)
- `RATELIMIT_DOWNLOAD_LIMIT`: Download endpoint limit (default: `"60/minute"`)
- `RATELIMIT_BATCH_LIMIT`: Batch job limit (default: `"20/minute"`)
- `RATELIMIT_STATUS_LIMIT`: Status/metrics endpoint limit (default: `"200/minute"`)

### Batch Processing
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: CPU×8, max 50)
- `BATCH_MAX_URLS_PER_BATCH`: Max URLs per batch (default: 50)

### PDF & Browser Pool Lifecycle
- `PDF_CONCURRENCY`: Max concurrent PDFs (default: CPU×2, max 12)
- `PDF_PAGE_LOAD_TIMEOUT`: Playwright timeout in ms (default: 30000)
- `PDF_MAX_USES_PER_BROWSER`: Recycle Chromium instance after N uses (default: 50)
- `PDF_IDLE_TIMEOUT`: Close idle browser pool after N seconds / scale-to-zero (default: 600.0)

### Logging & Security
- `LOG_JSON_LOGS`: Emit structured JSON logs with top-level `method`, `path`, and `status_code` on `uvicorn.access` (default: `false`; set `true` in production)
- `SSRF_BLOCK_PRIVATE_IPS`: Block private IPs across direct, redirect, `robots.txt`, and Playwright requests (default: `true`)
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: `http://localhost:3000,http://localhost:8080` — localhost only; set explicitly in production)

See `.env.example` and [`docs/guides/configuration.md`](docs/guides/configuration.md) for the complete configuration reference.

## 📦 Implementation Status (`v0.6.0`)

**✅ Production Ready Features:**
- ✅ Direct URL endpoint structure (`/{url}`) with `Accept` content negotiation (text, markdown, HTML, JSON, PDF)
- ✅ Stateless-first ethical crawling (`robots.txt`, `Crawl-delay`, `HTTP 403` `robots_disallowed`, Prometheus `robots_*` metrics)
- ✅ Streaming download size limits (`HTTP 413` `content_too_large`) and defense-in-depth SSRF protection (redirects + Playwright routes)
- ✅ Active moving-window rate limiting (`HTTP 429` + `Retry-After` + `X-RateLimit-*`)
- ✅ Lazy Playwright `BrowserPool` with max-use recycling (`50`) and idle scale-to-zero (`600s`)
- ✅ Background batch processing and cron-based scheduled downloads (`REDIS_URI` mode)
- ✅ Non-root Docker container on unprivileged port `:8000` with structured JSON access logging
- ✅ Health check and Prometheus/live metrics endpoints

**🚧 Roadmap (Deferred & Upcoming):**
- 📋 **Sprint 5 (Deferred until multi-tenant deployment):** Per-API-key `QuotaConfig`, `GET /usage`, and tenant bandwidth accounting
- 📋 **Sprint 6 Candidates:** Screenshot/PDF query toggle (`?output=screenshot|pdf`), webhook notifications, OCR (image-to-text), and typed client SDKs

See [`docs/roadmap.md`](docs/roadmap.md) and [`DECISIONS.md`](DECISIONS.md) for detailed milestones and architectural decisions.

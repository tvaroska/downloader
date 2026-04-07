# REST API Downloader

High-performance web service for programmatic URL content downloading with intelligent article extraction.

## 🚀 Features

- **Direct URL Access**: Simple `/{url}` endpoint structure
- **Background Batch Processing**: Asynchronous job-based processing with progress tracking
- **Content Negotiation**: Multiple response formats via Accept headers
- **PDF Generation**: JavaScript-rendered PDFs using Playwright
- **API Key Protection**: Optional authentication via environment variable
- **Intelligent Extraction**: BeautifulSoup-powered article text extraction
- **Security**: SSRF protection and URL validation
- **Performance**: Async HTTP client with connection pooling
- **Scheduled Downloads**: Cron-based recurring downloads with execution history
- **Production Ready**: Docker support with health checks
- **Comprehensive Testing**: 100% test coverage for critical components

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

## 🔥 Quick Start

### Using Docker (Recommended)

```bash
# Build the image
docker build -t downloader .

# Run without authentication
docker run -p 8000:8000 downloader

# Or run with API key authentication
docker run -e DOWNLOADER_KEY=your-secret-key -p 8000:8000 downloader

# Test the API
curl -H "Accept: text/plain" http://localhost:8000/https://example.com
```

### Local Development

```bash
# Install dependencies
uv sync

# Run development server
uv run python run.py

# Run tests
uv run pytest tests/ -v
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

## 🛡️ Security Features

- **SSRF Protection**: Blocks localhost and private IP ranges (configurable)
- **URL Validation**: Comprehensive input sanitization
- **Rate Limiting**: Multi-tier limits with Redis/in-memory storage (NEW)
  - Download endpoints: 60 requests/minute
  - Batch endpoints: 20 requests/minute
  - Status endpoints: 200 requests/minute
- **API Key Authentication**: Optional Bearer token or X-API-Key header
- **Non-root Container**: Docker runs with dedicated user account
- **DoS Protection**: Configurable concurrency limits and timeouts

## 🧪 Testing

The project uses a 3-tier test strategy for fast CI/CD:

```bash
# Quick smoke tests (<3s) - for rapid feedback
uv run pytest -m smoke -v

# Integration tests (~15s) - for component testing
uv run pytest -m integration -v

# Full E2E tests (~60s) - for comprehensive validation
uv run pytest -m e2e -v

# Run all tests with coverage
uv run pytest --cov=src --cov-report=html

# Test specific functionality
uv run pytest tests/api/test_download.py -v
```

**Test Statistics**: 248 tests (80 smoke, organized into smoke/integration/e2e tiers)

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

### HTTP Client
- `HTTP_MAX_CONNECTIONS`: Max total connections (default: 200)
- `HTTP_REQUEST_TIMEOUT`: Request timeout in seconds (default: 30)

### Rate Limiting (NEW)
- `RATELIMIT_ENABLED`: Enable rate limiting (default: true)
- `RATELIMIT_DEFAULT_LIMIT`: Default limit (default: "100/minute")
- `RATELIMIT_DOWNLOAD_LIMIT`: Download endpoint limit (default: "60/minute")
- `RATELIMIT_BATCH_LIMIT`: Batch job limit (default: "20/minute")

### Batch Processing
- `BATCH_CONCURRENCY`: Max concurrent batch requests (default: CPU×8, max 50)
- `BATCH_MAX_URLS_PER_BATCH`: Max URLs per batch (default: 50)

### PDF Generation
- `PDF_CONCURRENCY`: Max concurrent PDFs (default: CPU×2, max 12)
- `PDF_PAGE_LOAD_TIMEOUT`: Playwright timeout in ms (default: 30000)

### Security
- `SSRF_BLOCK_PRIVATE_IPS`: Block private IPs (default: true)
- `CORS_ALLOWED_ORIGINS`: Comma-separated origins (default: "*")

See `.env.example` for complete configuration reference.

## 📦 Implementation Status

**✅ Production Ready Features:**
- ✅ Direct URL endpoint structure (`/{url}`)
- ✅ Content negotiation via Accept headers (text, markdown, HTML, JSON, PDF)
- ✅ Background batch processing with job tracking
- ✅ PDF generation with Playwright
- ✅ Scheduled downloads with cron expressions and execution history
- ✅ **Rate limiting with DoS protection** (NEW)
- ✅ Comprehensive test suite (248 tests: 80 smoke, 3-tier strategy)
- ✅ SSRF protection and URL validation
- ✅ Optional API key authentication
- ✅ Production-ready Docker container
- ✅ Structured logging with JSON support
- ✅ Comprehensive configuration management (40+ settings)
- ✅ Health check and metrics endpoints

**🚧 Roadmap (Next Steps):**
- 🟠 High Priority (7-11 hours): HTTP client simplification, memory leak fixes, Docker improvements
- 🟡 Performance (2-4 weeks): Redis caching, webhook notifications, OpenTelemetry
- 🟢 Advanced (1-2 months): Enhanced content preprocessing, SDK libraries, GraphQL API
- 🔵 Enterprise (3-6 months): OAuth2/JWT, multi-region deployment, AI-powered extraction

See `docs/roadmap.md` for detailed implementation timeline.

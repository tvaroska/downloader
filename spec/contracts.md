# REST API Downloader - API Reference

**Last Updated:** 2026-11-12

Complete API documentation for the REST API Downloader service (`v0.7.0`).

## Base URL

```
http://localhost:8000
```

## Authentication

API key authentication is supported and can be enabled by setting the `DOWNLOADER_KEY` environment variable.

### Configuration

| Environment Variable | Description |
|---------------------|-------------|
| `DOWNLOADER_KEY` | API key for authentication. When set, all endpoints (including `/metrics*`) require a valid API key except `GET /health`, which remains public for liveness/readiness probes. |

> **OpenAPI & Interactive Docs Surface:** Interactive documentation (`/docs`, `/docs/oauth2-redirect`, `/redoc`) and `/openapi.json` are enabled in `development` and `staging`, and return `HTTP 404` when `ENVIRONMENT=production`.

### Authentication Methods

When authentication is enabled, provide your API key using one of these methods:

**Method 1: Bearer Token (Authorization header)**
```http
Authorization: Bearer your-api-key
```

**Method 2: X-API-Key Header**
```http
X-API-Key: your-api-key
```

### Authentication Errors

**401 Unauthorized - Missing API Key:**
```json
{
  "detail": {
    "success": false,
    "error": "API key required. Provide via Authorization header or X-API-Key header",
    "error_type": "authentication_required"
  }
}
```

**401 Unauthorized - Invalid API Key:**
```json
{
  "detail": {
    "success": false,
    "error": "Invalid API key",
    "error_type": "authentication_failed"
  }
}
```

### Disabling Authentication

To run the API without authentication (development/testing only), simply do not set the `DOWNLOADER_KEY` environment variable.

## Content Negotiation

The API uses HTTP Accept headers to determine response format. This allows the same endpoint to return different content types based on client preferences.

### Supported Formats

| Accept Header | Format | Description |
|--------------|--------|-------------|
| `text/plain` | Plain Text | Clean text with HTML tags stripped (or Tesseract OCR text for `image/*` origins) |
| `text/markdown` | Markdown | HTML converted to Markdown format (or Tesseract OCR Markdown for `image/*` origins) |
| `text/html` | HTML | Original or Playwright-rendered HTML content (or HTML-escaped OCR text for `image/*` origins) |
| `application/json` | JSON | Base64-encoded content with metadata |
| `application/pdf` | PDF | Page rendered as PDF document via Playwright |
| `image/png` | PNG Screenshot | Page rendered as PNG screenshot via Playwright |
| `image/jpeg` | JPEG Screenshot | Page rendered as JPEG screenshot via Playwright |
| (none/other) | Plain Text | Default fallback format |

### Multi-Format Requests

Request multiple formats in a single request by specifying comma-separated Accept values:

**Request:**
```http
GET /https://example.com
Accept: text/html, text/markdown
```

**Response:**
```json
{
  "text/html": "<!doctype html>...",
  "text/markdown": "# Example Domain\n\n..."
}
```

When multiple formats are requested, the response is always JSON containing each succeeded format. An `"errors"` object (`{"application/pdf": "..."}`) is included only when one or more requested formats fail.

## Endpoints

### Health Check

Check service health, version, deployment mode, connection drain state, and split-worker queue telemetry.

**Endpoint:** `GET /health`

**Healthy Response (`200 OK`):**
```json
{
  "status": "healthy",
  "draining": false,
  "active_requests": 0,
  "version": "0.7.0",
  "environment": "production",
  "deployment_mode": "all",
  "telemetry_enabled": false,
  "configuration": {
    "pdf_concurrency": 4,
    "batch_concurrency": 10,
    "max_download_size_mb": 50.0,
    "cpu_cores": 4,
    "respect_robots_txt": true,
    "user_agent": "DownloaderBot/0.7.0",
    "ocr_enabled": true,
    "webhooks_enabled": true
  },
  "services": {
    "job_manager": { "available": true, "status": "healthy" },
    "batch_processing": { "available": true },
    "pdf_generation": { "available": true },
    "scheduler": { "available": true },
    "workers": {
      "deployment_mode": "all",
      "remote_browser_enabled": false,
      "active_workers": 0,
      "workers": [],
      "queues": {
        "batch_pending": 0,
        "batch_processing": 0,
        "browser_pending": 0
      }
    }
  },
  "auth_enabled": true,
  "auth_methods": [
    "Authorization: Bearer <api_key>",
    "X-API-Key: <api_key>"
  ]
}
```

When `REDIS_URI` is configured, `services.workers` includes `active_workers`, `workers` (active worker heartbeats), and `queues` (`batch_pending`, `batch_processing`, `browser_pending`). When `DOWNLOADER_KEY` is unset, `auth_enabled` is `false` and `auth_methods` is `null`.

**Draining Response (`503 Service Unavailable` during graceful shutdown):**
```json
{
  "status": "draining",
  "draining": true,
  "active_requests": 2,
  "version": "0.7.0",
  "environment": "production",
  "deployment_mode": "all",
  "telemetry_enabled": false,
  "auth_enabled": true
}
```

**Status Codes:**
- `200` - Service is healthy and ready to accept traffic
- `503` - Service is draining active connections during graceful shutdown (non-`/health` endpoints also include `Connection: close` and `Retry-After: 5`)

---

### Download URL Content

Download and process content from any public URL with intelligent content extraction, OCR, screenshots, and declarative Playwright actions.

**Endpoint:** `GET /{url}`

**Parameters:**
- `url` (path, required) - The complete URL to download (e.g., `https://example.com`)

**Headers:**
- `Accept` (optional) - Content format preference (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, `application/json`, or comma-separated list for multi-format JSON)

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `render` | boolean | `false` | Force single-fetch JavaScript rendering with Playwright across `text/html`, `text/plain`, `text/markdown`, and multi-format requests (`X-Rendered-With-JS: true`). Uses single-pass `domcontentloaded` + bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`). Returns `HTTP 503` if browser concurrency slots are saturated, or falls back to static HTTP extraction (`X-Rendered-With-JS: false`) on unexpected browser failures. |
| `wait_for` | string | `null` | CSS selector to wait for before returning content. Implies `render=true`. Times out after `CONTENT_HTML_JS_RENDERING_TIMEOUT` (`10000` ms default) with `HTTP 408` (`selector_timeout_error`). Max 500 characters. |
| `output` | string | `null` | Explicit binary output override: `screenshot` or `pdf`. Overrides default text extraction regardless of `Accept`. |
| `full_page` | boolean | `false` | Capture the full scrollable page when rendering a screenshot (`output=screenshot` or `Accept: image/png|jpeg`). |
| `screenshot_format` | string | `png` | Image encoding for screenshot captures: `png` (`image/png`) or `jpeg` (`image/jpeg`). |
| `actions` | string | `null` | URL-encoded JSON array (max 8192 chars, max 20 actions) of strictly allowlisted declarative Playwright actions (`click`, `fill`, `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; zero raw JS evaluation). Implies `render=true`. |
| `ocr` | boolean | `false` | Require Tesseract OCR extraction (returns `HTTP 422` if origin is not a supported `image/*` content type, or `HTTP 503` `ocr_unavailable` if `tesseract` is unavailable or `OCR_ENABLED=false`). Image origins also run OCR automatically when `Accept` requests `text/plain`, `text/markdown`, or `text/html`. |
| `ocr_lang` | string | `null` | Optional Tesseract OCR language code (validated against `^[a-zA-Z0-9_+-]{1,32}$`, e.g., `eng`, `eng+deu`). |

**Common Response Headers:**
- `X-Original-URL`: Target URL
- `X-Original-Content-Type`: Origin MIME type
- `X-Content-Length`: Payload size in bytes
- `X-Rendered-With-JS`: `true` or `false` when Playwright rendering was invoked
- `X-OCR-Applied`: `true` when Tesseract OCR extracted text from an image origin
- `X-Trace-ID`: W3C 32-hex-char OpenTelemetry trace ID (present when `OTEL_ENABLED=true`)

**Supported Accept Header Values:**

#### Plain Text Response (`text/plain`)

Extracts main article content as clean text, removing navigation, ads, and other non-content elements.

**Request:**
```http
GET /https://example.com
Accept: text/plain
```

**Response:**
```
Example Domain This domain is for use in illustrative examples in documents. You may use this domain in literature without prior coordination or asking for permission. More information...
```

**Response Headers:**
- `Content-Type: text/plain; charset=utf-8`
- `X-Original-URL: https://example.com`
- `X-Original-Content-Type: text/html`
- `X-Content-Length: 1256`

#### Markdown Response (`text/markdown`)

Converts HTML content to structured Markdown format with headings, links, and lists preserved.

**Request:**
```http
GET /https://example.com
Accept: text/markdown
```

**Response:**
```markdown
# Example Domain

This domain is for use in illustrative examples in documents. You may use this domain in literature without prior coordination or asking for permission.

More information...

[More information...](https://www.iana.org/domains/example)
```

**Response Headers:**
- `Content-Type: text/markdown; charset=utf-8`
- `X-Original-URL: https://example.com`
- `X-Original-Content-Type: text/html`
- `X-Content-Length: 1256`

#### HTML Response (`text/html`)

Returns the original HTML content or converts non-HTML content to HTML format.

**Request:**
```http
GET /https://example.com
Accept: text/html
```

**Response:**
```html
<!doctype html>
<html>
<head>
    <title>Example Domain</title>
    <!-- ... -->
</head>
<body>
    <div>
        <h1>Example Domain</h1>
        <p>This domain is for use in illustrative examples...</p>
    </div>
</body>
</html>
```

**Response Headers:**
- `Content-Type: text/html` (or original content type)
- `X-Original-URL: https://example.com`
- `X-Original-Content-Type: text/html`
- `X-Content-Length: 1256`

#### JSON Response (`application/json`)

Returns structured JSON with base64-encoded content and comprehensive metadata.

**Request:**
```http
GET /https://example.com
Accept: application/json
```

**Response:**
```json
{
  "success": true,
  "url": "https://example.com",
  "size": 1256,
  "content_type": "text/html",
  "content": "PCFkb2N0eXBlIGh0bWw+CjxodG1sPgo8aGVhZD4KICAgIDx0aXRsZT5FeGFtcGxlIERvbWFpbjwvdGl0bGU+...",
  "metadata": {
    "url": "https://example.com",
    "status_code": 200,
    "content_type": "text/html",
    "size": 1256,
    "headers": {
      "content-type": "text/html; charset=UTF-8",
      "content-length": "1256",
      "server": "ECS (dcb/7F83)",
      "date": "Wed, 27 Jan 2025 21:00:00 GMT"
    }
  }
}
```

**Response Headers:**
- `Content-Type: application/json`
- `X-Original-URL: https://example.com`
- `X-Content-Length: 1256`

#### Default Response (Plain Text)

When no Accept header is specified, returns the content as plain text with intelligent article extraction. For unsupported Accept header formats, returns the raw content with original content type.

**Request:**
```http
GET /https://example.com
```

**Response:**
```
Example Domain This domain is for use in illustrative examples in documents. You may use this domain in literature without prior coordination or asking for permission. More information...
```

**Response Headers:**
- `Content-Type: text/plain; charset=utf-8`
- `X-Original-URL: https://example.com`
- `X-Original-Content-Type: text/html`
- `X-Content-Length: 1256`

---

### Scripted Browser Render (`POST /render`)

Execute up to `20` strictly allowlisted declarative Playwright actions (`click`, `fill`, `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; zero arbitrary JavaScript evaluation) prior to capturing text, Markdown, HTML, PDF, or a screenshot. Rate-limited under `RATELIMIT_DOWNLOAD_LIMIT` (`60/minute`).

**Endpoint:** `POST /render`

**Request Body:**
```json
{
  "url": "https://example.com",
  "output": "markdown",
  "wait_for": "main",
  "actions": [
    {"type": "click", "selector": "#accept-cookies"},
    {"type": "scroll", "y": 800},
    {"type": "wait_for_selector", "selector": ".loaded", "timeout_ms": 5000}
  ],
  "full_page": false,
  "screenshot_format": "png",
  "timeout_ms": 15000
}
```

**Request Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | Yes | Public `http`/`https` URL to render (SSRF- and `robots.txt`-checked) |
| `output` | string | No | Output format: `text`, `markdown`, `html`, `pdf`, or `screenshot` (default: `html`) |
| `wait_for` | string | No | Optional CSS selector to wait for before capture (max 500 chars) |
| `actions` | array | No | Up to `20` allowlisted actions: `click`, `fill`, `select`, `scroll`, `wait` (`ms <= 10000`), `wait_for_selector`, `hover`, `press` |
| `full_page` | boolean | No | Capture full scrollable page when `output="screenshot"` (default: `false`) |
| `screenshot_format` | string | No | `png` or `jpeg` when `output="screenshot"` (default: `png`) |
| `timeout_ms` | integer | No | Optional render timeout override in milliseconds (`1000..60000`) |

**Response:**
- Returns raw payload bytes with matching `Content-Type` (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `image/png`, or `image/jpeg`) and `X-Rendered-With-JS: true`.
- Returns `400` (`validation_error`) on disallowed actions or SSRF failures, `403` (`robots_disallowed`), `408` (`selector_timeout_error` / `timeout_error`), `503` (`service_unavailable` if browser slots saturated or `service_draining`), or `504` (`worker_timeout` in split `DEPLOYMENT_MODE=api`).

---

## Background Batch Processing

Process multiple URLs asynchronously with job tracking and result retrieval.

### Submit Batch Job

Submit a batch processing job for background execution.

**Endpoint:** `POST /batch`

**Request Body:**
```json
{
  "urls": [
    {
      "url": "https://example.com",
      "format": "text"
    },
    {
      "url": "https://github.com/python/cpython",
      "format": "markdown",
      "actions": [{"type": "scroll", "y": 500}]
    },
    {
      "url": "https://docs.python.org",
      "format": "screenshot"
    }
  ],
  "default_format": "text",
  "concurrency_limit": 10,
  "timeout_per_url": 30,
  "webhook_url": "https://hooks.example.com/batch-done",
  "webhook_secret": "<your-hmac-key>"
}
```

Supported per-URL `format` and `default_format` values: `text`, `markdown`, `html`, `pdf`, `screenshot`, `json`, `raw` (plus automatic Tesseract OCR for image origins when requesting `text`, `markdown`, or `html`).

- **`webhook_url` (optional):** Validated against SSRF at submission and delivery time. Fired strictly **after** `store_job_results` and `COMPLETED`/`FAILED` status persistence so receivers can immediately fetch `GET /jobs/{job_id}/results`. Sends `X-Downloader-Event: batch.completed` or `batch.failed` with `follow_redirects=False` and bounded retries (`WEBHOOK_MAX_RETRIES=3`).
- **`webhook_secret` (optional, write-only):** When provided, outbound deliveries include `X-Downloader-Signature-256: sha256=<hex>` (HMAC-SHA256 of the raw JSON body). Never returned in status or results responses.

**Response:**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "pending",
  "created_at": "2025-09-10T12:00:00Z",
  "total_urls": 3,
  "estimated_completion": "2025-09-10T12:01:00Z"
}
```

**Status Codes:**
- `200` - Job submitted successfully
- `400` - Invalid request (too many URLs, validation errors)
- `503` - Service unavailable (Redis not configured)

#### Implicit Playwright Fallback

When processing batch URLs with `text` or `markdown` format, the service automatically falls back to Playwright browser rendering if the initial HTML-to-text/markdown conversion produces empty content. This handles JavaScript-heavy pages (SPAs, React/Vue/Angular sites) without requiring `render=true`.

**How it works:**

1. The URL is fetched and converted to the requested format (text or markdown)
2. If the converted content is empty, the service checks whether Playwright fallback is appropriate:
   - The response must be HTML content
   - The HTML body must contain enough text (above a configurable threshold)
   - The page must have content indicators (`<main>`, `<article>`, `.content`, etc.)
   - Error/maintenance pages are excluded from fallback
3. If eligible, Playwright renders the page with `networkidle` wait strategy and extracts content from the rendered DOM

**Caching:** Results of fallback eligibility checks are cached per-URL with bounded LRU eviction and monotonic TTL (`CONTENT_CACHE_MAX_SIZE`, `CONTENT_HTML_JS_RENDERING_CACHE_TTL`) to avoid redundant Playwright invocations on repeated requests.

**Note:** Automatic JS/empty-body Playwright fallback applies to both `GET /{url}` and `POST /batch` when `CONTENT_ENABLE_HTML_JS_RENDERING=true` (degrading gracefully to static content when `PDF_CONCURRENCY` browser slots are saturated). Use `?render=true` or `?wait_for=<selector>` on `GET /{url}` when deterministic browser execution is required. `Accept: application/pdf` (and `format: "pdf"` in `/batch` and `/schedules`) renders directly in Playwright without a redundant pre-flight HTTP fetch, propagating upstream `4xx` responses as `HTTP <status>` (`error_type: "http_error"`).

---

### Check Job Status

Get the current status and progress of a batch processing job.

**Endpoint:** `GET /jobs/{job_id}/status`

**Parameters:**
- `job_id` (path, required) - Job identifier returned from batch submission

**Response:**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "running",
  "progress": 67,
  "created_at": "2025-09-10T12:00:00Z",
  "started_at": "2025-09-10T12:00:05Z",
  "completed_at": null,
  "total_urls": 3,
  "processed_urls": 2,
  "successful_urls": 2,
  "failed_urls": 0,
  "error_message": null,
  "results_available": false,
  "expires_at": "2025-09-11T12:00:00Z"
}
```

**Job Status Values:**
- `pending` - Job queued for processing
- `running` - Job currently being processed
- `completed` - Job finished successfully
- `failed` - Job failed with errors
- `cancelled` - Job was cancelled

**Status Codes:**
- `200` - Status retrieved successfully
- `404` - Job not found or expired

---

### Download Job Results

Download the results of a completed batch processing job.

**Endpoint:** `GET /jobs/{job_id}/results`

**Parameters:**
- `job_id` (path, required) - Job identifier

**Response:**
```json
{
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "status": "completed",
  "total_duration": 45.2,
  "results": [
    {
      "url": "https://example.com",
      "success": true,
      "format": "text",
      "content": "Example Domain This domain is for use...",
      "size": 1234,
      "content_type": "text/html",
      "duration": 1.2,
      "status_code": 200
    },
    {
      "url": "https://github.com/python/cpython",
      "success": true,
      "format": "markdown",
      "content": "# CPython\n\nThe Python programming language...",
      "size": 5678,
      "content_type": "text/html",
      "duration": 2.1,
      "status_code": 200
    },
    {
      "url": "https://docs.python.org",
      "success": true,
      "format": "pdf",
      "content_base64": "JVBERi0xLjQKJcfsj6IKNSAwIG9iago8PAovVHlwZSAvQ2F0YWxvZwo...",
      "size": 89012,
      "content_type": "application/pdf",
      "duration": 8.5,
      "status_code": 200
    }
  ],
  "summary": {
    "total_requests": 3,
    "successful_requests": 3,
    "failed_requests": 0,
    "success_rate": 100.0,
    "total_duration": 45.2
  },
  "created_at": "2025-09-10T12:00:00Z",
  "completed_at": "2025-09-10T12:00:45Z"
}
```

**Response Headers:**
- `Content-Type: application/json`
- `Content-Disposition: attachment; filename="batch_results_[job_id].json"`
- `X-Job-ID: [job_id]`
- `X-Job-Status: completed`
- `X-Total-Duration: 45.2`

**Status Codes:**
- `200` - Results downloaded successfully
- `400` - Results not available (job still running, failed, etc.)
- `404` - Job not found or results expired

---

### Cancel Job

Cancel a running or pending batch processing job.

**Endpoint:** `DELETE /jobs/{job_id}`

**Parameters:**
- `job_id` (path, required) - Job identifier

**Response:**
```json
{
  "success": true,
  "message": "Job a1b2c3d4-e5f6-7890-abcd-ef1234567890 cancelled successfully"
}
```

**Status Codes:**
- `200` - Cancellation processed (check response body for actual result)
- `404` - Job not found

---

## Metrics

Monitor service performance, health, and resource usage.

### Prometheus Metrics

Get all service metrics in Prometheus text exposition format for scraping.

**Endpoint:** `GET /metrics`

**Response:**
```
Content-Type: text/plain; version=0.0.4; charset=utf-8

# HELP downloader_requests_total Total requests
# TYPE downloader_requests_total counter
downloader_requests_total 1234
# HELP downloader_request_duration_seconds Request duration
# TYPE downloader_request_duration_seconds histogram
downloader_request_duration_seconds_sum 456.78
downloader_request_duration_seconds_count 1234
# HELP downloader_errors_total Total errors
# TYPE downloader_errors_total counter
downloader_errors_total 12
# HELP robots_evaluations_total Total robots.txt evaluations
# TYPE robots_evaluations_total counter
robots_evaluations_total 850
# HELP robots_cache_hits_total Total robots.txt cache hits
# TYPE robots_cache_hits_total counter
robots_cache_hits_total 790
# HELP robots_disallowed_total Total URLs disallowed by robots.txt
# TYPE robots_disallowed_total counter
robots_disallowed_total 4
```

**Status Codes:**
- `200` - Metrics retrieved successfully

---

### Performance Summary

Get detailed performance statistics per endpoint.

**Endpoint:** `GET /metrics/performance`

**Response:**
```json
{
  "uptime_seconds": 3600.5,
  "total_requests": 1234,
  "total_errors": 12,
  "overall_error_rate_percent": 0.97,
  "overall_avg_response_time": 0.37,
  "endpoints": {
    "GET_/{url}": {
      "request_count": 800,
      "error_count": 10,
      "error_rate_percent": 1.25,
      "avg_response_time": 0.45,
      "min_response_time": 0.05,
      "max_response_time": 2.1,
      "p95_response_time": 1.2
    }
  }
}
```

**Status Codes:**
- `200` - Performance data retrieved successfully

---

### Health Score

Get a composite health score with diagnostic breakdown.

**Endpoint:** `GET /metrics/health-score`

**Response:**
```json
{
  "overall_score": 92,
  "status": "healthy",
  "factors": {
    "error_rate": {
      "value": 0.97,
      "penalty": 5.0,
      "status": "good"
    },
    "response_time": {
      "value": 0.37,
      "penalty": 3.0,
      "status": "good"
    },
    "uptime": {
      "value": 3600.5,
      "bonus": 0,
      "status": "good"
    }
  }
}
```

**Health Status Values:**
- `healthy` - Score >= 80
- `degraded` - Score >= 50
- `unhealthy` - Score < 50

**Status Codes:**
- `200` - Health score retrieved successfully

---

### Live System Metrics

Get real-time system resource usage and concurrency statistics.

**Endpoint:** `GET /metrics/live`

**Response:**
```json
{
  "timestamp": 1737460800.0,
  "concurrency": {
    "pdf": {
      "limit": 4,
      "active": 1,
      "available": 3,
      "utilization_percent": 25.0
    },
    "batch": {
      "limit": 16,
      "active": 3,
      "available": 13,
      "utilization_percent": 18.8
    }
  },
  "performance": {
    "uptime_seconds": 3600.5,
    "total_requests": 1234,
    "total_errors": 12,
    "overall_error_rate_percent": 0.97,
    "overall_avg_response_time": 0.37
  },
  "connections": {
    "redis": {
      "status": "not_configured"
    },
    "http_client": {
      "status": "healthy"
    }
  },
  "system": {
    "cpu_cores": 4,
    "pdf_scaling": "2x CPU cores (max 12), current: 4",
    "batch_scaling": "8x CPU cores (max 50), current: 16"
  }
}
```

**Status Codes:**
- `200` - Live metrics retrieved successfully

---

## Error Handling

All errors return JSON responses with structured error information.

### Error Response Format

```json
{
  "detail": {
    "success": false,
    "error": "Error description",
    "error_type": "error_category"
  }
}
```

### Error Types and Status Codes

#### URL Validation Errors (400 Bad Request)

**Error Type:** `validation_error`

**Common Cases:**
- Invalid URL format
- Missing or malformed URL
- Blocked domains (localhost, private IPs)

**Example:**
```json
{
  "detail": {
    "success": false,
    "error": "Invalid URL format: missing scheme",
    "error_type": "validation_error"
  }
}
```

#### Ethical Crawling (`robots.txt`) Disallowed Errors (403 Forbidden)

**Error Type:** `robots_disallowed`

Returned on `GET /{url}` when `RESPECT_ROBOTS_TXT=true` and the origin's `/robots.txt` disallows the path for the configured `USER_AGENT` (`REST-API-Downloader/1.0` default). In `POST /batch`, disallowed URLs record `success: false`, `status_code: 403`, and the disallow message per item while allowed URLs in the same batch continue processing. When `/robots.txt` is unreachable, times out, returns `4xx`/`5xx`, or is malformed, the service **fails open** and caches an allow sentinel rather than blocking downloads.

```json
{
  "detail": {
    "success": false,
    "error": "Disallowed by robots.txt for User-Agent 'REST-API-Downloader/1.0': https://example.com/private",
    "error_type": "robots_disallowed"
  }
}
```

#### Content Size Limit Errors (413 Content Too Large)

**Error Type:** `content_too_large`

Returned on `GET /{url}` (and recorded with `status_code: 413` per item in `POST /batch`) when an upstream `Content-Length` header or streamed response body exceeds `CONTENT_MAX_DOWNLOAD_SIZE` (`52428800` bytes / 50MB default). Streaming transfers are aborted immediately without buffering the remainder of the response.

```json
{
  "detail": {
    "success": false,
    "error": "Content size exceeds maximum allowed size of 52428800 bytes",
    "error_type": "content_too_large"
  }
}
```

#### HTTP Client Errors

**404 Not Found:**
```json
{
  "detail": {
    "success": false,
    "error": "HTTP 404: Not Found",
    "error_type": "http_error"
  }
}
```

**403 Forbidden:**
```json
{
  "detail": {
    "success": false,
    "error": "HTTP 403: Forbidden",
    "error_type": "http_error"
  }
}
```

**502 Bad Gateway:**
```json
{
  "detail": {
    "success": false,
    "error": "HTTP 500: Internal Server Error",
    "error_type": "http_error"
  }
}
```

#### Timeout Errors (408 Request Timeout)

**Error Type:** `timeout_error`

```json
{
  "detail": {
    "success": false,
    "error": "Request timed out after 30 seconds",
    "error_type": "timeout_error"
  }
}
```

#### Download Errors (500 Internal Server Error)

**Error Type:** `download_error`

```json
{
  "detail": {
    "success": false,
    "error": "Failed to download content",
    "error_type": "download_error"
  }
}
```

#### Internal Errors (500 Internal Server Error)

**Error Type:** `internal_error`

```json
{
  "detail": {
    "success": false,
    "error": "Internal server error",
    "error_type": "internal_error"
  }
}
```

#### OCR Processing & Availability Errors (`422` / `503`)

- **`422 Unprocessable Entity` (`error_type: "ocr_error"`):** Returned when `?ocr=true` targets a non-image origin, an image fails decoding, or decoded dimensions exceed `OCR_MAX_PIXELS` (`25_000_000` default decompression-bomb guard).
- **`503 Service Unavailable` (`error_type: "ocr_unavailable"`):** Returned when `OCR_ENABLED=false` or the `tesseract` system binary is unavailable on the host.

#### Graceful Shutdown & Split-Worker Errors (`503` / `504`)

- **`503 Service Unavailable` (`error_type: "service_draining"`):** Returned with `Connection: close` and `Retry-After: 5` by `DrainMiddleware` once pod shutdown begins while in-flight requests drain.
- **`504 Gateway Timeout` (`error_type: "worker_timeout"`):** Returned when `DEPLOYMENT_MODE=api` dispatches a browser render/PDF/screenshot task to a remote worker via Redis and no worker completes it within `WORKER_BROWSER_TASK_TIMEOUT`.

## Rate Limiting

Rate limiting is enabled by default to prevent abuse and ensure fair resource allocation.

### Default Rate Limits

| Endpoint Pattern | Limit | Description |
|-----------------|-------|-------------|
| `/health`, `/metrics*`, `/jobs/*` | 200/minute | Lightweight status, polling, and metrics endpoints |
| `/{url}` (download) | 60/minute | Resource-intensive download operations |
| `/batch` | 20/minute | Async batch job creation |
| All other endpoints | 100/minute | Default limit |

Rate-limit keys isolate authenticated callers (`key:<sha256[:16]>:<ip>`) from unauthenticated traffic (`unauth:<ip>`) so invalid-credential floods cannot exhaust valid API key quotas. Client IPs are resolved right-to-left from `X-Forwarded-For` only when the immediate peer IP belongs to `RATELIMIT_TRUSTED_PROXIES`.

### Storage Backend

Rate limiting supports two storage backends:

| Backend | Configuration | Use Case |
|---------|---------------|----------|
| Redis | Set `RATELIMIT_STORAGE_URI` or `REDIS_URI` | Distributed rate limiting across multiple instances |
| In-memory | Default (no config needed) | Single instance deployments |

### Rate Limit Headers

When rate limiting is enabled, responses include the following headers:

| Header | Description |
|--------|-------------|
| `X-RateLimit-Limit` | Maximum requests allowed in the time window |
| `X-RateLimit-Remaining` | Requests remaining in current window |
| `X-RateLimit-Reset` | Unix timestamp when the rate limit resets |
| `Retry-After` | Seconds until the client may retry (included on `429` responses) |

### Rate Limit Exceeded Response

**429 Too Many Requests** (includes `Retry-After` and `X-RateLimit-*` response headers):
```json
{
  "detail": {
    "success": false,
    "error": "Rate limit exceeded: 60 per 1 minute",
    "error_type": "rate_limit_exceeded"
  }
}
```

### Configuration

Rate limits can be customized via environment variables:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `RATELIMIT_ENABLED` | `true` | Enable/disable rate limiting |
| `RATELIMIT_DEFAULT_LIMIT` | `100/minute` | Default rate limit |
| `RATELIMIT_DOWNLOAD_LIMIT` | `60/minute` | Download endpoint limit |
| `RATELIMIT_BATCH_LIMIT` | `20/minute` | Batch endpoint limit |
| `RATELIMIT_STATUS_LIMIT` | `200/minute` | Status endpoint limit |
| `RATELIMIT_STORAGE_URI` | None | Redis URI for distributed limiting |
| `RATELIMIT_HEADERS_ENABLED` | `true` | Include rate limit headers |
| `RATELIMIT_TRUSTED_PROXIES` | `127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` | Trusted proxy IPs/CIDRs allowed to supply `X-Forwarded-For` |

## Security & Ethical Crawling Features

### SSRF Protection

The API enforces async DNS/IP SSRF validation and DNS-pinned TCP socket connections (`SSRFSafeHTTPTransport` with `trust_env=False`) across initial URL pre-flight, every `3xx` HTTP redirect `Location` target, `POST /batch` and `POST /schedules` submissions, `robots.txt` origin fetches, and Playwright subresource/navigation routes (`context.route("**/*", ...)`), blocking:
- Localhost (`127.0.0.1`, `localhost`, `*.localhost`, `::1`) and alternate numeric/hex/octal/shorthand IPv4 or IPv4-mapped IPv6 encodings (`::ffff:127.0.0.1`)
- Private IP ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
- Link-local & cloud metadata addresses (`169.254.0.0/16`, `169.254.169.254`, `::ffff:169.254.169.254`)
- Non-HTTP(S) schemes such as `file://` (Playwright inline `data:`, `blob:`, and `about:` resources remain allowed)
- Prohibited hop-by-hop or framing custom headers (`Host`, `Content-Length`, `Transfer-Encoding`, `Proxy-Authorization`, etc.)

### Stateless-First Ethical Crawling (`robots.txt` & `Crawl-delay`)

Before executing `GET /{url}`, each item in `POST /batch`, or any scheduled job in `/schedules`, the service evaluates `{scheme}://{netloc}/robots.txt` and enforces per-domain `Crawl-delay` pacing when `RESPECT_ROBOTS_TXT=true`:

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `USER_AGENT` | `REST-API-Downloader/1.0` | `User-Agent` sent on HTTP fetches, `/robots.txt` lookups, and Playwright contexts |
| `RESPECT_ROBOTS_TXT` | `true` | Enable `robots.txt` and `Crawl-delay` enforcement (`false` disables) |
| `ROBOTS_CACHE_TTL` | `3600` | Cache TTL in seconds for parsed rules and fail-open sentinels |
| `ROBOTS_CACHE_MAX_SIZE` | `1000` | Max origins in bounded in-memory LRU cache |
| `ROBOTS_FETCH_TIMEOUT` | `5.0` | Timeout in seconds when fetching `/robots.txt` |
| `MAX_CRAWL_DELAY` | `30.0` | Maximum `Crawl-delay` honored per domain in seconds |

- **Storage backends:** Uses bounded in-memory TTL/LRU caching and per-domain monotonic slot locks by default when `REDIS_URI` is unset, and coordinates via Redis (`robots:v2:{scheme}:{netloc}`, atomic Lua `throttle:v1:{domain}`) when `REDIS_URI` is configured.
- **Fail-open behavior:** Unreachable, timed-out, `4xx`/`5xx`, or malformed `robots.txt` responses fail open with a structured warning log and cache an allow sentinel for `ROBOTS_CACHE_TTL`.

### Input & Streaming Size Validation

- URL format validation and scheme sanitization (only `http` and `https` allowed)
- Streaming `CONTENT_MAX_DOWNLOAD_SIZE` enforcement (`50MB` default -> `413` `content_too_large` via `Content-Length` pre-check and chunked byte counter)
- `User-Agent` sanitization

## Content Extraction

### Article Detection

The intelligent content extraction targets common article containers:
- `<article>` elements
- `<main>` elements
- Elements with `role="main"`
- Common class names: `.content`, `.post-content`, `.entry-content`, `.article-content`

### Content Cleaning

Automatically removes:
- Navigation elements (`<nav>`)
- Headers and footers (`<header>`, `<footer>`)
- Sidebars (`<aside>`)
- Scripts and styles (`<script>`, `<style>`)
- Forms and interactive elements
- Advertisements and tracking elements

## SDKs and Client Libraries

First-party typed client SDKs covering `GET /{url}`, `POST /render`, screenshots, OCR, `/batch` + webhooks, and `/schedules` live under `sdks/`:

- **Python (`httpx` + Pydantic v2, sync & async):** [`sdks/python/README.md`](../sdks/python/README.md)
- **JavaScript / TypeScript (ESM, built-in `fetch`, zero runtime deps):** [`sdks/javascript/README.md`](../sdks/javascript/README.md)
- **Go (`net/http` stdlib, zero external deps):** [`sdks/go`](../sdks/go)

### cURL Examples

```bash
# Get article text (with authentication)
curl -H "Accept: text/plain" \
     -H "Authorization: Bearer your-api-key" \
     "http://localhost:8000/https://news.ycombinator.com"

# Get markdown format (using X-API-Key header)
curl -H "Accept: text/markdown" \
     -H "X-API-Key: your-api-key" \
     "http://localhost:8000/https://github.com/python/cpython"

# Get JSON with metadata
curl -H "Accept: application/json" \
     -H "Authorization: Bearer your-api-key" \
     "http://localhost:8000/https://httpbin.org/json"

# Get original HTML
curl -H "Accept: text/html" \
     -H "Authorization: Bearer your-api-key" \
     "http://localhost:8000/https://example.com"
```

### Python Example

```python
import requests
import base64

API_KEY = "your-api-key"
BASE_URL = "http://localhost:8000"

# Get article text
response = requests.get(
    f"{BASE_URL}/https://example.com",
    headers={
        "Accept": "text/plain",
        "Authorization": f"Bearer {API_KEY}"
    }
)
article_text = response.text

# Get JSON with metadata
response = requests.get(
    f"{BASE_URL}/https://example.com",
    headers={
        "Accept": "application/json",
        "X-API-Key": API_KEY
    }
)
data = response.json()
content = base64.b64decode(data["content"])
```

### JavaScript Example

```javascript
const API_KEY = "your-api-key";
const BASE_URL = "http://localhost:8000";

// Get article text
const response = await fetch(`${BASE_URL}/https://example.com`, {
  headers: {
    "Accept": "text/plain",
    "Authorization": `Bearer ${API_KEY}`
  }
});
const articleText = await response.text();

// Get JSON with metadata
const jsonResponse = await fetch(`${BASE_URL}/https://example.com`, {
  headers: {
    "Accept": "application/json",
    "X-API-Key": API_KEY
  }
});
const data = await jsonResponse.json();
const content = atob(data.content); // Decode base64
```

## Performance Considerations

### Response Times

- Simple text extraction: ~100-500ms
- Complex article extraction: ~200-800ms
- Large files (>1MB): ~1-3 seconds

### Memory Usage

- Streaming support for large files
- Automatic content cleanup
- Connection pooling for efficiency

### Caching

Currently no caching is implemented. Future versions will include:
- Redis-based response caching
- TTL-based cache invalidation
- Cache-Control header support

## Browser Rendering (JavaScript/SPA Support)

The API supports rendering JavaScript-heavy pages using Playwright with headless Chromium. This enables scraping of Single Page Applications (SPAs), React/Vue/Angular sites, and any page that requires JavaScript execution.

### Auto-Detection

By default, the API attempts to auto-detect pages that require JavaScript rendering. However, for reliable results with SPAs, use explicit rendering parameters.

### Force Rendering with `?render=true`

Use `?render=true` to force JavaScript rendering, bypassing auto-detection:

```bash
# Render a React application
curl -H "Accept: text/markdown" \
     "http://localhost:8000/https://react-app.example.com?render=true"

# Get rendered HTML from a Vue.js SPA
curl -H "Accept: text/html" \
     "http://localhost:8000/https://vuejs-app.example.com?render=true"
```

### Wait for Dynamic Content with `?wait_for=<selector>`

For SPAs that load content dynamically after initial page load, use `?wait_for=` to wait for a specific CSS selector before extracting content:

```bash
# Wait for main content container to appear
curl -H "Accept: text/plain" \
     "http://localhost:8000/https://spa.example.com?wait_for=.main-content"

# Wait for article to load in a news site
curl -H "Accept: text/markdown" \
     "http://localhost:8000/https://news.example.com/article?wait_for=article"

# Wait for data table to populate
curl -H "Accept: text/html" \
     "http://localhost:8000/https://dashboard.example.com?wait_for=#data-table"
```

**Note:** When `wait_for` is specified, `render=true` is automatically enabled.

### SPA Scraping Examples

#### React Application

```bash
# Get content from a React app after hydration
curl -H "Accept: text/markdown" \
     "http://localhost:8000/https://reactjs.org/docs?render=true&wait_for=.docs-content"
```

#### Vue.js Application

```bash
# Wait for Vue component to mount
curl -H "Accept: text/plain" \
     "http://localhost:8000/https://vuejs.org/guide?wait_for=#main-content"
```

#### Angular Application

```bash
# Wait for Angular app to bootstrap
curl -H "Accept: text/html" \
     "http://localhost:8000/https://angular.io/docs?wait_for=app-root"
```

#### Infinite Scroll / Lazy Loading

```bash
# Wait for initial items to load
curl -H "Accept: text/markdown" \
     "http://localhost:8000/https://infinite-scroll.example.com?wait_for=.item-list"
```

### Timeout Handling

The `wait_for` parameter uses `CONTENT_HTML_JS_RENDERING_TIMEOUT` (`10000` ms default). If the selector is not found within this time, the API returns a 408 Request Timeout error:

```json
{
  "detail": {
    "success": false,
    "error": "Timeout waiting for selector '.non-existent-element' after 10000ms",
    "error_type": "selector_timeout_error"
  }
}
```

### Browser Session Limits

- **Timeout:** 30 seconds per browser session (`PDF_PAGE_LOAD_TIMEOUT=30000`)
- **Memory limit:** 512MB per browser context (`PDF_BROWSER_MEMORY_LIMIT_MB=512`)
- **Instance recycling:** Automatic Chromium replacement after `50` uses (`PDF_MAX_USES_PER_BROWSER=50`) to prevent V8 heap fragmentation
- **Idle scale-to-zero:** Browser pool closes automatically after `600` seconds of inactivity (`PDF_IDLE_TIMEOUT=600.0`) and restarts lazily on the next render or PDF request
- **Concurrency:** Managed via internal browser pool and semaphore

### Python Example with Rendering

```python
import requests

BASE_URL = "http://localhost:8000"

# Scrape a React SPA
response = requests.get(
    f"{BASE_URL}/https://react-app.example.com",
    params={
        "render": "true",
        "wait_for": ".app-container"
    },
    headers={"Accept": "text/markdown"}
)
content = response.text
```

### JavaScript Example with Rendering

```javascript
const BASE_URL = "http://localhost:8000";

// Scrape a Vue.js SPA
const url = new URL(`${BASE_URL}/https://vuejs-app.example.com`);
url.searchParams.set("render", "true");
url.searchParams.set("wait_for", "#app");

const response = await fetch(url, {
  headers: { "Accept": "text/markdown" }
});
const content = await response.text();
```

## Scheduled Downloads

Schedule recurring downloads with cron expressions. Schedules are stored in Redis and execute automatically at the specified times.

### Create Schedule

Create a new scheduled download job.

**Endpoint:** `POST /schedules`

**Request Body:**
```json
{
  "name": "Daily Report Download",
  "url": "https://example.com/report",
  "cron_expression": "0 9 * * *",
  "format": "markdown",
  "headers": {"Authorization": "Bearer token"},
  "enabled": true
}
```

**Request Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Human-readable name (1-100 characters) |
| `url` | string | Yes | URL to download (1-2048 characters) |
| `cron_expression` | string | Yes | Standard 5-field cron expression |
| `format` | string | No | Output format: `text`, `html`, `markdown`, `pdf`, `screenshot`, `json`, `raw` (default: `text`) |
| `headers` | object | No | Custom HTTP headers for the request (prohibited hop-by-hop headers rejected with `400`) |
| `enabled` | boolean | No | Whether schedule is active (default: `true`) |
| `webhook_url` | string | No | Optional SSRF-validated callback URL notified with `schedule.completed` or `schedule.failed` after execution finishes |
| `webhook_secret` | string | No | Optional write-only HMAC-SHA256 signing secret (`X-Downloader-Signature-256: sha256=<hex>`); never returned in schedule responses |

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Daily Report Download",
  "url": "https://example.com/report",
  "cron_expression": "0 9 * * *",
  "format": "markdown",
  "headers": {"Authorization": "Bearer token"},
  "enabled": true,
  "webhook_url": "https://hooks.example.com/schedules",
  "created_at": "2026-01-21T10:30:00Z",
  "next_run_time": "2026-01-22T09:00:00Z"
}
```

**Status Codes:**
- `200` - Schedule created successfully
- `400` - Invalid cron expression, SSRF/URL validation failure, or prohibited custom header (`validation_error`)
- `503` - Scheduler service unavailable (Redis not connected)

**Error Examples:**

Invalid cron expression:
```json
{
  "detail": {
    "success": false,
    "error": "Invalid cron expression: Wrong number of fields",
    "error_type": "validation_error"
  }
}
```

Scheduler unavailable:
```json
{
  "detail": {
    "success": false,
    "error": "Scheduler service is not available. Redis connection required.",
    "error_type": "service_unavailable"
  }
}
```

---

### List Schedules

Get all scheduled download jobs.

**Endpoint:** `GET /schedules`

**Response:**
```json
{
  "schedules": [
    {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "name": "Daily Report Download",
      "url": "https://example.com/report",
      "cron_expression": "0 9 * * *",
      "format": "markdown",
      "headers": null,
      "enabled": true,
      "created_at": "2026-01-21T10:30:00Z",
      "next_run_time": "2026-01-22T09:00:00Z"
    }
  ],
  "total": 1
}
```

**Status Codes:**
- `200` - Schedules retrieved successfully
- `503` - Scheduler service unavailable

---

### Get Schedule Details

Get details of a specific scheduled job.

**Endpoint:** `GET /schedules/{schedule_id}`

**Parameters:**
- `schedule_id` (path, required) - Schedule identifier (UUID)

**Response:**
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "Daily Report Download",
  "url": "https://example.com/report",
  "cron_expression": "0 9 * * *",
  "format": "markdown",
  "headers": {"Authorization": "Bearer token"},
  "enabled": true,
  "created_at": "2026-01-21T10:30:00Z",
  "next_run_time": "2026-01-22T09:00:00Z"
}
```

**Status Codes:**
- `200` - Schedule retrieved successfully
- `404` - Schedule not found
- `503` - Scheduler service unavailable

---

### Get Schedule History

Get execution history for a scheduled job.

**Endpoint:** `GET /schedules/{schedule_id}/history`

**Parameters:**
- `schedule_id` (path, required) - Schedule identifier (UUID)

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `limit` | integer | `20` | Maximum results per page (1-100) |
| `offset` | integer | `0` | Number of results to skip |

**Response:**
```json
{
  "executions": [
    {
      "execution_id": "abc12345-6789-0def-ghij-klmnopqrstuv",
      "schedule_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "completed",
      "started_at": "2026-01-21T09:00:00Z",
      "completed_at": "2026-01-21T09:00:02Z",
      "duration": 2.5,
      "success": true,
      "content_size": 15234,
      "error_message": null,
      "attempt": 1
    },
    {
      "execution_id": "def67890-abcd-1234-efgh-ijklmnopqrst",
      "schedule_id": "550e8400-e29b-41d4-a716-446655440000",
      "status": "failed",
      "started_at": "2026-01-20T09:00:00Z",
      "completed_at": "2026-01-20T09:00:35Z",
      "duration": 35.2,
      "success": false,
      "content_size": null,
      "error_message": "Connection timeout after 3 attempts",
      "attempt": 3
    }
  ],
  "total": 2,
  "limit": 20,
  "offset": 0
}
```

**Execution Status Values:**
- `pending` - Execution queued
- `running` - Currently executing
- `completed` - Finished successfully
- `failed` - Failed after all retry attempts

**Status Codes:**
- `200` - History retrieved successfully
- `404` - Schedule not found
- `503` - Execution storage unavailable

---

### Delete Schedule

Remove a scheduled job.

**Endpoint:** `DELETE /schedules/{schedule_id}`

**Parameters:**
- `schedule_id` (path, required) - Schedule identifier (UUID)

**Response:**
```json
{
  "success": true,
  "message": "Schedule 550e8400-e29b-41d4-a716-446655440000 deleted successfully"
}
```

**Status Codes:**
- `200` - Schedule deleted successfully
- `404` - Schedule not found
- `503` - Scheduler service unavailable

---

### Cron Expression Format

Schedules use standard 5-field UNIX cron format:

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *
```

**Common Patterns:**

| Pattern | Expression | Description |
|---------|-----------|-------------|
| Daily at 9 AM | `0 9 * * *` | Every day at 9:00 AM |
| Weekdays at 9 AM | `0 9 * * 1-5` | Monday-Friday at 9:00 AM |
| Every 15 minutes | `*/15 * * * *` | At 0, 15, 30, 45 minutes |
| Hourly | `0 * * * *` | At the start of every hour |
| Every 6 hours | `0 */6 * * *` | At midnight, 6 AM, noon, 6 PM |
| Weekly on Sunday | `0 0 * * 0` | Every Sunday at midnight |
| First of month | `0 0 1 * *` | 1st day of each month at midnight |

**Supported Operators:**
- `*` - Any value
- `*/n` - Every n units (step)
- `n-m` - Range from n to m
- `n,m,o` - Specific values

---

### Retry Behavior

Failed scheduled jobs are automatically retried for transient errors:

- **Maximum attempts:** 3
- **Retry delays:** 5s, 15s, 30s (worker concurrency slots are released before backoff sleep)
- **Non-retryable policy errors:** `URLValidationError`, `SSRFProtectionError`, and `RobotsDisallowedError` fail immediately on attempt 1 without retrying
- **Final status:** After exhausting allowed attempts, execution is recorded as `failed` in history

---

### Scheduler Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `SCHEDULER_JOB_STORE_TYPE` | `redis` | APScheduler job store backend (`redis` or `memory`; `REDIS_URI` is required to enable `/schedules` and execution history storage) |
| `SCHEDULER_MAX_WORKERS` | `10` | Maximum concurrent scheduled download attempts |
| `SCHEDULER_MISFIRE_GRACE_TIME` | `60` | Seconds late a job can still run |
| `SCHEDULER_COALESCE` | `true` | Run once if multiple executions missed |

---

### Operational Configuration (`v0.7.0`)

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `OCR_ENABLED` | `true` | Enable Tesseract image-to-text OCR (`false` returns `503 ocr_unavailable` on `?ocr=true`) |
| `OCR_DEFAULT_LANGUAGE` | `eng` | Default Tesseract language pack |
| `OCR_TIMEOUT_SECONDS` | `30.0` | Timeout per OCR extraction in seconds |
| `OCR_MAX_PIXELS` | `25000000` | Maximum decoded image pixels (decompression-bomb guard) |
| `WEBHOOK_ENABLED` | `true` | Enable outbound batch/schedule webhook delivery |
| `WEBHOOK_TIMEOUT_SECONDS` | `10.0` | Timeout per outbound webhook attempt in seconds |
| `WEBHOOK_MAX_RETRIES` | `3` | Retry attempts on transient `5xx`/network failures |
| `SHUTDOWN_DRAIN_TIMEOUT` | `30.0` | Max seconds to wait for active requests and jobs during shutdown |
| `SHUTDOWN_POLL_INTERVAL` | `0.25` | Poll interval in seconds while waiting for drain completion |
| `DEPLOYMENT_MODE` | `all` | Runtime topology: `all` (monolith/stateless default), `api` (offloads batch/browser via Redis), or `worker` |
| `WORKER_POLL_TIMEOUT` | `5.0` | Worker Redis `BLMOVE` poll interval in seconds |
| `WORKER_BROWSER_TASK_TIMEOUT` | `60.0` | Timeout waiting for remote worker Playwright/PDF/screenshot RPC |
| `WORKER_MAX_CONCURRENT_JOBS` | `4` | Max concurrent batch jobs processed per worker instance |
| `OTEL_ENABLED` | `false` | Opt-in OpenTelemetry distributed tracing (`false` adds zero runtime overhead) |
| `OTEL_SERVICE_NAME` | `downloader` | OpenTelemetry service name resource attribute |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `null` | OTLP HTTP exporter endpoint (e.g., `http://otel-collector:4318`) |
| `OTEL_CONSOLE_EXPORTER` | `false` | Emit OpenTelemetry spans to stdout for local debugging |

---

## Versioning

Current API version: `v1.0` (service `0.7.0`)

Version information available via `/health` endpoint.

Future versions will include versioning in the URL path: `/v2/{url}`

## Support

- GitHub Issues: Report bugs and request features
- Documentation: Complete guides and examples
- Community: Best practices and use cases

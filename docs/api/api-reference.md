# REST API Downloader - API Reference

Complete API documentation for the REST API Downloader service.

## Base URL

```
http://localhost:8000
```

## Authentication

API key authentication is supported and can be enabled by setting the `DOWNLOADER_KEY` environment variable.

### Configuration

| Environment Variable | Description |
|---------------------|-------------|
| `DOWNLOADER_KEY` | API key for authentication. When set, all requests must include a valid API key. |

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
| `text/plain` | Plain Text | Clean text with HTML tags stripped |
| `text/markdown` | Markdown | HTML converted to Markdown format |
| `text/html` | HTML | Original or rendered HTML content |
| `application/json` | JSON | Base64-encoded content with metadata |
| `application/pdf` | PDF | Page rendered as PDF document |
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
  "text/markdown": "# Example Domain\n\n...",
  "errors": {}
}
```

When multiple formats are requested, the response is always JSON containing all requested formats.

## Endpoints

### Health Check

Check service health and version information.

**Endpoint:** `GET /health`

**Response:**
```json
{
  "status": "healthy",
  "version": "0.0.1"
}
```

**Status Codes:**
- `200` - Service is healthy

---

### Download URL Content

Download and process content from any public URL with intelligent content extraction.

**Endpoint:** `GET /{url}`

**Parameters:**
- `url` (path, required) - The complete URL to download (e.g., `https://example.com`)

**Headers:**
- `Accept` (optional) - Content format preference

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `render` | boolean | `false` | Force JavaScript rendering with Playwright. Bypasses auto-detection and renders the page in a headless browser. |
| `wait_for` | string | `null` | CSS selector to wait for before returning content. Implies `render=true`. Times out after 10 seconds with 408 error. Max 500 characters. |

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
      "format": "markdown"
    },
    {
      "url": "https://docs.python.org",
      "format": "pdf"
    }
  ],
  "default_format": "text",
  "concurrency_limit": 10,
  "timeout_per_url": 30
}
```

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

## Rate Limiting

Rate limiting is enabled by default to prevent abuse and ensure fair resource allocation.

### Default Rate Limits

| Endpoint Pattern | Limit | Description |
|-----------------|-------|-------------|
| `/health`, `/metrics` | 200/minute | Lightweight status endpoints |
| `/{url}` (download) | 60/minute | Resource-intensive download operations |
| `/batch` | 20/minute | Async batch job creation |
| All other endpoints | 100/minute | Default limit |

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

### Rate Limit Exceeded Response

**429 Too Many Requests:**
```json
{
  "detail": {
    "success": false,
    "error": "Rate limit exceeded. Try again later.",
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

## Security Features

### SSRF Protection

The API automatically blocks requests to:
- Localhost (`127.0.0.1`, `localhost`)
- Private IP ranges (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`)
- Link-local addresses (`169.254.0.0/16`)

### Input Validation

- URL format validation
- Scheme validation (only `http` and `https` allowed)
- Content-length limits
- User-Agent sanitization

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

The `wait_for` parameter has a 10-second timeout. If the selector is not found within this time, the API returns a 408 Request Timeout error:

```json
{
  "detail": {
    "success": false,
    "error": "Timeout waiting for selector '.non-existent-element' after 10s",
    "error_type": "timeout_error"
  }
}
```

### Browser Session Limits

- **Timeout:** 30 seconds per browser session
- **Memory limit:** 512MB per browser context
- **Concurrency:** Managed via internal browser pool

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

## Versioning

Current API version: `v1.0`

Version information available via `/health` endpoint.

Future versions will include versioning in the URL path: `/v2/{url}`

## Support

- GitHub Issues: Report bugs and request features
- Documentation: Complete guides and examples
- Community: Best practices and use cases

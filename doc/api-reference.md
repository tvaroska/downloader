# REST API Downloader - API Reference

Complete API documentation for the REST API Downloader service.

## Base URL

```
http://localhost:8000
```

## Authentication

Currently, no authentication is required. Future versions will support API key authentication.

## Content Negotiation

The API uses HTTP Accept headers to determine response format. This allows the same endpoint to return different content types based on client preferences.

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

Currently, no rate limiting is implemented. Future versions will include:
- Per-IP rate limiting
- API key-based rate limiting
- Configurable rate limits

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
# Get article text
curl -H "Accept: text/plain" "http://localhost:8000/https://news.ycombinator.com"

# Get markdown format
curl -H "Accept: text/markdown" "http://localhost:8000/https://github.com/python/cpython"

# Get JSON with metadata
curl -H "Accept: application/json" "http://localhost:8000/https://httpbin.org/json"

# Get original HTML
curl -H "Accept: text/html" "http://localhost:8000/https://example.com"
```

### Python Example

```python
import requests

# Get article text
response = requests.get(
    "http://localhost:8000/https://example.com",
    headers={"Accept": "text/plain"}
)
article_text = response.text

# Get JSON with metadata
response = requests.get(
    "http://localhost:8000/https://example.com",
    headers={"Accept": "application/json"}
)
data = response.json()
content = base64.b64decode(data["content"])
```

### JavaScript Example

```javascript
// Get article text
const response = await fetch("http://localhost:8000/https://example.com", {
  headers: { "Accept": "text/plain" }
});
const articleText = await response.text();

// Get JSON with metadata
const jsonResponse = await fetch("http://localhost:8000/https://example.com", {
  headers: { "Accept": "application/json" }
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

## Versioning

Current API version: `v1.0`

Version information available via `/health` endpoint.

Future versions will include versioning in the URL path: `/v2/{url}`

## Support

- GitHub Issues: Report bugs and request features
- Documentation: Complete guides and examples
- Community: Best practices and use cases

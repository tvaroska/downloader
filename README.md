# REST API Downloader

High-performance web service for programmatic URL content downloading with intelligent article extraction.

## 🚀 Features

- **Direct URL Access**: Simple `/{url}` endpoint structure
- **Content Negotiation**: Multiple response formats via Accept headers
- **PDF Generation**: JavaScript-rendered PDFs using Playwright
- **API Key Protection**: Optional authentication via environment variable
- **Intelligent Extraction**: BeautifulSoup-powered article text extraction
- **Security**: SSRF protection and URL validation
- **Performance**: Async HTTP client with connection pooling
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

- **SSRF Protection**: Blocks localhost and private IP ranges
- **URL Validation**: Comprehensive input sanitization
- **Rate Limiting Ready**: Architecture supports rate limiting implementation
- **Non-root Container**: Docker runs with dedicated user account

## 🧪 Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=src --cov-report=html

# Test specific functionality
uv run pytest tests/test_api.py::TestDownloadEndpoint::test_download_text_format -v
```

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

Environment variables:
- `PYTHONUNBUFFERED=1`: Real-time logging output
- `UV_CACHE_DIR`: Custom cache directory for uv

## 📦 Implementation Status

**✅ Completed Features:**
- Direct URL endpoint structure (`/{url}`)
- Content negotiation via Accept headers
- BeautifulSoup article extraction
- Text, HTML, markdown, and JSON response formats
- Comprehensive test suite (33 tests passing)
- Production-ready Docker container
- SSRF protection and security measures
- Health check endpoint
- Error handling with proper HTTP status codes

**🚧 Roadmap:**
- Batch processing capabilities
- Redis caching layer
- Rate limiting implementation
- Advanced authentication
- Webhook notifications
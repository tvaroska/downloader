# Configuration Management Implementation

**Last Updated:** 2026-11-01

## Summary

Implemented comprehensive configuration management system using Pydantic Settings with:
- ✅ Centralized configuration with type-safe validation
- ✅ Documented magic numbers with rationale
- ✅ Structured logging with separate access and error handlers
- ✅ `.env.example` file for easy setup
- ✅ Dependency injection for configuration access

**Completion Date**: 2025-10-02
**Effort**: ~8 hours
**Status**: ✅ **COMPLETED**

---

## 🎯 Problems Solved

### 1. **Scattered Environment Variables** ✅ FIXED
**Before**: `os.getenv()` calls scattered across 5+ files
**After**: Centralized `Settings` class with clear structure

### 2. **Magic Numbers Without Documentation** ✅ FIXED
**Before**: Hardcoded values like `12`, `50`, `100`, `200` with no explanation
**After**: All values documented with rationale in config.py

### 3. **No Configuration Validation** ✅ FIXED
**Before**: Invalid config could crash app at runtime
**After**: Pydantic validation at startup with clear error messages

### 4. **Basic Logging** ✅ ENHANCED
**Before**: Simple `basicConfig()` with single handler
**After**: Structured JSON logging with separate access/error handlers

### 5. **No Configuration Documentation** ✅ FIXED
**Before**: No documentation of environment variables
**After**: Comprehensive `.env.example` with examples and rationale

---

## 📁 Files Created

### 1. `src/downloader/config.py` (45+ settings)
Centralized configuration using Pydantic Settings with:

- **HTTPClientConfig**: Connection pooling, timeouts, concurrency, redirect SSRF limits
- **PDFConfig**: PDF generation settings, Playwright concurrency, `PDF_MAX_USES_PER_BROWSER` (`50`), and `PDF_IDLE_TIMEOUT` (`600s` scale-to-zero)
- **BatchConfig**: Batch processing limits and concurrency
- **ContentConfig**: Streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`50MB` -> `HTTP 413`), cache settings, JS-heavy domain heuristics
- **CrawlingConfig**: Stateless-first ethical crawling (`USER_AGENT`, `RESPECT_ROBOTS_TXT`, `ROBOTS_CACHE_TTL`, `ROBOTS_CACHE_MAX_SIZE`, `ROBOTS_FETCH_TIMEOUT`, `MAX_CRAWL_DELAY`)
- **RedisConfig**: Redis connection and pooling
- **SchedulerConfig**: Cron worker and misfire settings
- **AuthConfig**: API key authentication
- **LoggingConfig**: Log levels, JSON formatting (`CustomJsonFormatter` emitting top-level `method`, `path`, `status_code` on `uvicorn.access`), rotation
- **SSRFConfig**: Security settings for pre-flight, redirect, `robots.txt`, and Playwright SSRF protection
- **CORSConfig**: CORS allowed origins
- **RateLimitConfig**: Moving-window rate limit tiers (`HTTP 429` + `Retry-After` + `X-RateLimit-*`)
- **Settings**: Main settings class combining all configs

**Key Features**:
- All magic numbers documented with rationale
- Pydantic validation with clear error messages
- Environment variable support with `.env` file
- Type-safe with full type hints
- Validation warnings for production settings

### 2. `src/downloader/logging_config.py` (189 lines)
Structured logging configuration with:

- **Separate Handlers**:
  - Access logs → stdout (or file)
  - Error logs → stderr (or file)
- **JSON Structured Logging**: Production-ready with searchable fields
- **Log Rotation**: Configurable size-based rotation
- **Custom Formatters**: Rich context in JSON logs
- **Logger Utilities**: Helper functions for structured logging

### 3. `.env.example` (180 lines)
Comprehensive configuration template with:

- All environment variables documented
- Rationale for each setting
- Quick start examples for:
  - Development (default)
  - Production
  - High performance (16GB RAM)
  - Memory constrained (2GB VPS)

---

## 🔧 Files Modified

### 1. `src/downloader/main.py`
**Changes**:
- ❌ Removed: `_get_optimal_concurrency_limits()` function
- ❌ Removed: `os.getenv()` calls for `PDF_CONCURRENCY`, `BATCH_CONCURRENCY`, `REDIS_URI`
- ❌ Removed: Basic `logging.basicConfig()`
- ✅ Added: Config loading via `get_settings()`
- ✅ Added: Structured logging via `setup_logging()`
- ✅ Added: Settings stored in `app.state.settings`
- ✅ Added: Configuration validation on startup with warnings
- ✅ Added: HTTPClient initialized with config values
- ✅ Added: Semaphores use config values (`settings.pdf.concurrency`, `settings.batch.concurrency`)
- ✅ Added: CORS uses config (`settings.cors.allowed_origins`)
- ✅ Added: Health endpoint shows environment and config values

### 2. `src/downloader/auth.py`
**Changes**:
- ❌ Removed: `get_api_key_from_env()` function
- ❌ Removed: `os.getenv("DOWNLOADER_KEY")` calls
- ✅ Added: `Settings` parameter to functions
- ✅ Added: Config-based authentication via `settings.auth.api_key`
- ✅ Updated: All functions accept optional `Settings` parameter

### 3. `src/downloader/dependencies.py`
**Changes**:
- ✅ Added: `get_settings_dependency()` function
- ✅ Added: `SettingsDep` type alias for dependency injection
- ✅ Added: Import of config module

### 4. `pyproject.toml`
**Changes**:
- ✅ Added: `pydantic-settings>=2.0.0`
- ✅ Added: `python-json-logger>=2.0.0`

---

## 📊 Configuration Statistics

### Magic Numbers Documented

| Category | Count | Examples |
|----------|-------|----------|
| HTTP Client | 8 | keepalive=100, max_connections=200, timeout=30s |
| PDF Generation | 5 | concurrency=2x CPU (max 12), timeout=10s, pool=3 |
| Batch Processing | 4 | concurrency=8x CPU (max 50), max_urls=50 |
| Content | 3 | max_size=50MB, cache=1000, cleanup=3600s |
| Logging | 5 | rotation_size=10MB, rotation_count=5 |

**Total Magic Numbers Documented**: 25+

### Environment Variables

| Category | Variables | Required |
|----------|-----------|----------|
| Application | 3 | No |
| HTTP Client | 8 | No (smart defaults) |
| PDF Generation | 4 | No (auto-calculated) |
| Batch Processing | 3 | No (auto-calculated) |
| Content | 3 | No |
| Redis | 2 | No (optional feature) |
| Authentication | 1 | No (optional feature) |
| Logging | 7 | No |
| SSRF Protection | 3 | No (defaults secure) |
| CORS | 1 | No (defaults permissive) |

**Total Configuration Options**: 35+
**Required Variables**: 0 (all optional with sensible defaults)

---

## 🚀 Usage Examples

### Basic Usage (Development)
```bash
# No configuration needed! Sensible defaults for everything
uvicorn src.downloader.main:app --reload
```

### Production Configuration
```bash
# Create .env file
cp .env.example .env

# Edit .env
ENVIRONMENT=production
DOWNLOADER_KEY=your-secret-key-here
REDIS_URI=redis://redis-host:6379
LOG_JSON_LOGS=true
LOG_ACCESS_LOG_FILE=/var/log/downloader/access.log
LOG_ERROR_LOG_FILE=/var/log/downloader/error.log
CORS_ALLOWED_ORIGINS=https://yourdomain.com
```

### Accessing Configuration in Code
```python
# Via dependency injection (recommended)
from src.downloader.dependencies import SettingsDep

@app.get("/example")
async def example(settings: SettingsDep):
    max_size = settings.content.max_download_size
    return {"max_size_mb": max_size / 1024 / 1024}

# Direct access
from src.downloader.config import get_settings

settings = get_settings()
print(f"PDF concurrency: {settings.pdf.concurrency}")
```

### Structured Logging
```python
from src.downloader.logging_config import get_logger, log_with_context
import logging

logger = get_logger(__name__)

# Regular logging
logger.info("Download started")

# Structured logging with context (great for JSON logs)
log_with_context(
    logger, logging.INFO,
    "Download completed",
    url="https://example.com",
    status_code=200,
    size_bytes=1024,
    duration_ms=234
)
```

---

## ✅ Benefits Achieved

### 1. **Type Safety**
- Pydantic validates all configuration at startup
- Invalid values caught immediately with clear error messages
- IDE autocomplete for all settings

### 2. **Documentation**
- Every magic number explained with rationale
- `.env.example` provides complete reference
- Quick start examples for different scenarios

### 3. **Maintainability**
- Single source of truth for all configuration
- Easy to add new settings
- Clear structure: `settings.category.setting`

### 4. **Production Ready**
- Structured JSON logging for monitoring
- Separate access and error logs
- Log rotation out of the box
- Environment-specific configuration

### 5. **Security**
- Production warnings for insecure configuration
- SSRF protection configurable
- CORS configurable (defaults warn in production)
- API key management centralized

### 6. **Performance**
- Auto-calculated concurrency based on CPU cores
- Configurable limits for different deployment sizes
- Memory-constrained and high-performance presets

### 7. **Testability**
- `reload_settings()` for test isolation
- Easy to mock configuration
- Dependency injection makes testing clean

---

## 🔍 Validation Features

### Startup Validation
The application validates configuration on startup and logs warnings:

```
INFO: Starting REST API Downloader v0.0.1
INFO: Environment: production
WARNING: CORS allows all origins in production
INFO: PDF concurrency: 8
INFO: Batch concurrency: 32
INFO: Max download size: 50.0MB
INFO: Redis: enabled
INFO: Auth: enabled
```

### Cross-Field Validation
```python
# Example: max_connections must be >= max_keepalive_connections
if max_connections < max_keepalive_connections:
    raise ValueError(f"max_connections ({max_connections}) must be >= max_keepalive_connections")
```

### Environment-Specific Warnings
```python
if environment == "production":
    if not api_key:
        logger.warning("No API key configured in production")
    if "*" in cors_allowed_origins:
        logger.warning("CORS allows all origins in production")
```

---

## 📈 Metrics & Impact

### Code Quality
- **Before**: Environment variables scattered across 5+ files
- **After**: Single `config.py` with 35+ documented settings
- **Improvement**: ⭐⭐⭐⭐⭐ (5/5)

### Maintainability
- **Before**: No documentation of magic numbers
- **After**: Every value documented with rationale
- **Improvement**: ⭐⭐⭐⭐⭐ (5/5)

### Production Readiness
- **Before**: Basic logging, no validation
- **After**: Structured logs, config validation, warnings
- **Improvement**: ⭐⭐⭐⭐⭐ (5/5)

### Developer Experience
- **Before**: Trial and error to configure
- **After**: `.env.example` with examples, type hints, validation
- **Improvement**: ⭐⭐⭐⭐⭐ (5/5)

---

## 🎓 Key Learnings

### 1. **Auto-Calculated Defaults**
CPU-based concurrency calculation:
```python
# PDF: 2x CPU cores (CPU-bound with I/O wait)
default_pdf = min(cpu_count * 2, 12)

# Batch: 8x CPU cores (I/O-bound, more parallelism)
default_batch = min(cpu_count * 8, 50)
```

### 2. **Memory Footprint Rationale**
```python
# Why max 50MB per download?
# 50MB × 50 concurrent = 2.5GB max
# Prevents memory exhaustion while handling large documents

# Why max 12 PDF browsers?
# 12 browsers × ~250MB = ~3GB max
# Balances throughput vs memory on typical VMs
```

### 3. **Structured Logging Best Practices**
- **Access logs** (who/when/what) → stdout → monitoring
- **Error logs** (problems/debugging) → stderr → alerting
- **JSON in production** → searchable, parseable
- **Human-readable in dev** → easy debugging

---

## 🔄 Migration Guide

For existing deployments, no code changes required. The system uses sensible defaults for all settings.

### Optional: Environment Variables
If you were using environment variables before, they still work:

```bash
# Old way (still works)
export PDF_CONCURRENCY=8
export BATCH_CONCURRENCY=32
export REDIS_URI=redis://localhost:6379
export DOWNLOADER_KEY=secret

# New way (recommended - use .env file)
# All the same, but now documented in .env.example
```

### New Features Available
```bash
# Structured JSON logging
LOG_JSON_LOGS=true
LOG_ACCESS_LOG_FILE=/var/log/downloader/access.log
LOG_ERROR_LOG_FILE=/var/log/downloader/error.log

# CORS configuration
CORS_ALLOWED_ORIGINS=https://yourdomain.com,https://admin.yourdomain.com

# Download size limits
CONTENT_MAX_DOWNLOAD_SIZE=104857600  # 100MB

# And 25+ more configuration options!
```

---

## 📝 Related Recommendations

This implementation addresses:

- ✅ **Recommendation 7 (High Priority)**: Configuration Management
- ✅ **Recommendation 2.3 (Medium Priority)**: Magic Numbers and Configuration
- ✅ **Recommendation 12 (Medium Priority)**: Structured Logging (partial - file handlers)
- ✅ **Recommendation 15 (Low Priority)**: Add Configuration Documentation

---

## Rate Limiting Storage Fallback & Enforcement

`RateLimitMiddleware` enforces moving-window rate limits (`status_limit` for `/health*` and `/metrics*`, `batch_limit` for `/batch*` and `/jobs*`, `download_limit` for `/{url}` downloads, and `default_limit` for other endpoints) and emits `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset` headers (plus `HTTP 429` `rate_limit_exceeded` and `Retry-After` when exceeded).

The rate limiter storage backend is resolved using a three-step fallback chain:

1. **`RATELIMIT_STORAGE_URI`** — if set, used directly. Useful when you want a dedicated Redis instance for rate limiting separate from job storage.
2. **`REDIS_URI`** — if `RATELIMIT_STORAGE_URI` is not set, the main Redis connection is reused.
3. **`memory://`** — if neither is set, falls back to in-memory storage. This works for single-instance deployments but **resets on restart** and does **not share state** across multiple instances.

The fallback is implemented in `src/downloader/main.py`:
```python
storage_uri = settings.ratelimit.storage_uri or settings.redis.redis_uri or "memory://"
```

> **Tip:** For production multi-instance deployments, ensure at least `REDIS_URI` is configured so rate limits are enforced globally.

---

## Ethical Crawling, Browser Recycling & Streaming Guards (`v0.6.0`)

### Stateless-First Ethical Crawling (`CrawlingConfig`)

| Environment Variable | Default | Range | Description |
|---------------------|---------|-------|-------------|
| `USER_AGENT` | `REST-API-Downloader/1.0` | string | Outbound `User-Agent` sent by `HTTPClient`, `RobotsChecker`, and Playwright contexts |
| `RESPECT_ROBOTS_TXT` | `true` | bool | Evaluate `/robots.txt` and `Crawl-delay` before `GET /{url}` and `POST /batch` (`false` opts out) |
| `ROBOTS_CACHE_TTL` | `3600` | `60..86400` | TTL in seconds for cached `robots.txt` rules and fail-open sentinels |
| `ROBOTS_CACHE_MAX_SIZE` | `1000` | `10..50000` | Max origins retained in bounded in-memory `OrderedDict` LRU cache |
| `ROBOTS_FETCH_TIMEOUT` | `5.0` | `1.0..60.0` | Timeout in seconds when fetching origin `/robots.txt` |
| `MAX_CRAWL_DELAY` | `30.0` | `0.0..300.0` | Maximum `Crawl-delay` honored per domain in seconds |

- **Cache & Throttle Backend Parity:** When `REDIS_URI` is unset, `RobotsChecker` and `DomainThrottle` operate 100% in-memory with bounded LRU state. When `REDIS_URI` is configured, rules and domain delay slots coordinate across pods via `robots:v1:{netloc}` and `throttle:v1:{domain}`, falling back cleanly to memory on Redis errors.
- **Disallowed vs Fail-Open Behavior:** Disallowed URLs return `HTTP 403` (`error_type: "robots_disallowed"` on `GET /{url}`, or per-URL `status_code=403` in `/batch`). Unreachable, timed-out, `4xx`/`5xx`, or malformed `robots.txt` responses fail open with a structured warning log and cache an `allow_all` sentinel for `ROBOTS_CACHE_TTL`.

### Browser Pool Recycling & Scale-to-Zero (`PDFConfig`)
- **`PDF_MAX_USES_PER_BROWSER` (`default: 50`, `1..10000`):** Automatically replaces a Chromium instance once its usage count reaches `50` to prevent V8 heap fragmentation on long-lived pods.
- **`PDF_IDLE_TIMEOUT` (`default: 600.0`, `0.0..86400.0`):** Closes the shared `BrowserPool` after 10 minutes of zero active contexts (`scale-to-zero`) and lazily re-initializes Chromium on the next PDF or rendered-HTML request.

### Streaming Size Limit & Structured Access Logs
- **`CONTENT_MAX_DOWNLOAD_SIZE` (`default: 52428800` / 50MB):** `HTTPClient` checks `Content-Length` headers prior to reading body bytes and counts chunks during streaming downloads, aborting immediately with `HTTP 413` (`content_too_large`) when exceeded.
- **`LOG_JSON_LOGS=true` (`CustomJsonFormatter`):** Automatically extracts top-level `method`, `path` (query-stripped, e.g. `"/health"`), and `status_code` fields on `uvicorn.access` records so log ingestion filters can match health probes cleanly.

---

## 🚧 Future Enhancements

### Potential Additions
1. **Configuration Hot Reload**: Reload settings without restart (requires app architecture changes)
2. **Remote Configuration**: Load from config server (etcd, Consul)
3. **Configuration Versioning**: Track config changes over time
4. **Configuration UI**: Web interface for configuration management
5. **Environment Profiles**: Pre-defined profiles (dev, staging, production)

### Already Planned (Other Recommendations)
- **Rate Limiting Configuration** (Recommendation 3)
- **SSRF Protection Enhancement** (Recommendation 2.4)
- **Log Rotation Service** (complete Recommendation 12)

---

## ✅ Acceptance Criteria

All criteria met:

- [x] Centralized configuration with Pydantic Settings
- [x] All magic numbers documented with rationale
- [x] `.env.example` file with comprehensive documentation
- [x] Type-safe configuration with validation
- [x] Structured logging with separate handlers
- [x] Configuration accessible via dependency injection
- [x] No breaking changes to existing deployments
- [x] Production-ready with security warnings
- [x] Zero required configuration (sensible defaults)
- [x] Comprehensive documentation

---

## 🎉 Conclusion

The configuration management system is now:
- **Type-safe** with Pydantic validation
- **Well-documented** with rationale for all values
- **Production-ready** with structured logging
- **Developer-friendly** with .env.example and sensible defaults
- **Secure** with validation warnings
- **Maintainable** with clear structure

**Total Implementation Time**: ~8 hours
**Lines of Code**: ~1,000+ lines (config + logging + docs)
**Configuration Options**: 35+ documented settings
**Magic Numbers Eliminated**: 25+

**Status**: ✅ **PRODUCTION READY**

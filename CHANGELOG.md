# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-01-21

### Added
- Cron-based job scheduling with APScheduler integration
- Schedule CRUD endpoints: `POST/GET/DELETE /schedules`, `GET /schedules/{id}`
- Job execution with configurable retry logic (max 3 attempts)
- Job history endpoint: `GET /schedules/{id}/history` with pagination (default 20, max 100)
- Redis-based job store for schedule persistence across restarts
- 114 scheduler tests (78 unit + 36 integration) with 95% module coverage

### Changed
- Scheduler service initializes on app startup with Redis job store
- Job results stored in Redis with 24h TTL

### Technical
- New modules: `scheduler/service.py`, `scheduler/executor.py`, `scheduler/storage.py`
- New routes: `routes/schedules.py`
- New models: `models/schedule.py`

## [0.3.0] - 2026-01-19

### Added
- Browser rendering with Playwright: `?render=true` query parameter for JavaScript-heavy pages
- CSS selector waiting: `?wait_for=<selector>` parameter for dynamic content loading (10s timeout)
- Browser pool management with configurable memory limits (512MB per session)
- HTML to Markdown conversion with markdownify library
- Plain text extraction with BeautifulSoup
- Content negotiation via Accept headers (`text/markdown`, `text/plain`, `text/html`)
- Batch support with `format` and `default_format` options
- GitHub Actions CI pipeline with automated testing
- Production monitoring dashboard with Prometheus/Grafana integration
- 46 integration tests for browser rendering and security
- 69 transformer tests for markdown and plaintext conversion

### Changed
- Extracted BrowserPool to dedicated module (`browser/manager.py`)
- Consolidated version management to single source in `pyproject.toml`
- Fixed unbounded caches with LRU BoundedCache implementation
- Docker image updated to Python 3.13

### Fixed
- Zombie process cleanup with timeout-based forced termination
- Process isolation for browser instances
- Test timeouts to prevent hanging tests

### Security
- Blocked `file://` URL scheme in browser rendering to prevent local file access
- Removed `--disable-web-security` browser flag
- Added `--disable-webgl` hardening flag
- Memory limits enforced via `--js-flags` (512MB max per context)

## [0.2.1] - 2026-01-02

### Fixed
- Added brotli dependency for JavaScript rendering on Substack and modern sites using compressed content

## [0.2.0] - 2026-01-02

### Added
- Multiple format support in single request (comma-separated Accept headers)
- JavaScript rendering for HTML responses
- Content type detection and handling
- Base64 encoding for JSON responses

## [0.1.5] - 2025-11-17

### Changed
- Various stability improvements and bug fixes

## [0.1.0] - 2025-10-04

### Added
- Initial release
- Direct URL endpoint structure (`GET /{url}`)
- Background batch processing with job tracking (`POST /batch`)
- Job status and results endpoints (`GET /jobs/{id}/status`, `GET /jobs/{id}/results`)
- PDF generation with Playwright
- API key authentication (optional via `DOWNLOADER_KEY`)
- Rate limiting with configurable per-endpoint limits
- SSRF protection blocking private IPs and cloud metadata endpoints
- Health check endpoint (`GET /health`)
- Metrics endpoints for Prometheus integration
- Docker containerization with health checks
- Comprehensive test suite

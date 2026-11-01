# Ethical Crawling & Stateless Safety - Feature History

**Last Updated:** 2026-11-01

## Overview

Stateless-first `robots.txt` compliance, configurable `User-Agent` propagation, per-domain `Crawl-delay` egress pacing, and `v0.6.0` production hardening (streaming download size enforcement, redirect and Playwright SSRF blocking, browser lifecycle recycling/scale-to-zero, active rate-limit enforcement, structured JSON access-log path extraction, and unprivileged `:8000` container networking).

---

## Status: ✅ Complete (Sprint 4 — v0.6.0)

Sprint 4 completed: 2026-11-01

---

## Completed Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Configurable `USER_AGENT` & `RESPECT_ROBOTS_TXT` | P1 | Low | ✅ Complete |
| `robots.txt` parser + bounded in-memory TTL/LRU cache (optional Redis) | P1 | Medium | ✅ Complete |
| Per-domain `Crawl-delay` throttling (in-memory + optional Redis) | P1 | Medium | ✅ Complete |
| `GET /{url}` & `POST /batch` pre-flight enforcement | P1 | Medium | ✅ Complete |
| Structured `HTTP 403` `robots_disallowed` responses & Prometheus counters | P1 | Low | ✅ Complete |
| Production HTTP, SSRF, Playwright, rate-limit, logging & `:8000` hardening | P0 | Medium | ✅ Complete |

### Configuration & Wire Behavior Delivered
- `CrawlingConfig` (`src/downloader/config.py`): `USER_AGENT` (`default: REST-API-Downloader/1.0`), `RESPECT_ROBOTS_TXT` (`default: true`), `ROBOTS_CACHE_TTL` (`default: 3600`), `ROBOTS_CACHE_MAX_SIZE` (`default: 1000`), `ROBOTS_FETCH_TIMEOUT` (`default: 5.0`), and `MAX_CRAWL_DELAY` (`default: 30.0`).
- Stateless-first caching & throttling: bounded in-memory LRU/TTL cache (`OrderedDict`) and per-domain monotonic slot locks work out-of-the-box when `REDIS_URI` is unset, with optional shared Redis keys (`robots:v1:{netloc}`, `throttle:v1:{domain}`) when `REDIS_URI` is configured.
- Fail-open resilience: unreachable, slow, `4xx`/`5xx`, or malformed `robots.txt` responses log a structured warning and cache a fail-open sentinel (`allow_all=True`) so origin outages never stall downloads.
- Disallowed responses: `GET /{url}` returns `HTTP 403` (`ErrorResponse` with `error_type: "robots_disallowed"`); `POST /batch` records `status_code: 403` per disallowed URL while allowed URLs continue processing.
- Prometheus `/metrics` & `/metrics/live` counters: `robots_evaluations_total`, `robots_cache_hits_total`, and `robots_disallowed_total`.

### Production Hardening Delivered (`v0.6.0`)
- **Streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`):** `HTTPClient` validates `Content-Length` headers pre-read and aborts chunked streams immediately once accumulated bytes exceed `max_download_size`, mapping `ContentTooLargeError` to `HTTP 413` (`error_type: "content_too_large"`).
- **Redirect & Playwright SSRF Protection:** Every `3xx` HTTP redirect `Location` target (including relative redirects) and every Playwright subresource/navigation request (`context.route("**/*", ...)`) is validated via `validate_url()`, blocking private RFC1918, loopback, link-local/cloud metadata (`169.254.169.254`), and `file://` targets.
- **Active Rate Limiting (`HTTP 429`):** `RateLimitMiddleware` enforces moving-window limits across status (`200/minute`), download (`60/minute`), batch (`20/minute`), and default (`100/minute`) tiers, emitting `X-RateLimit-{Limit,Remaining,Reset}` headers and `HTTP 429` (`error_type: "rate_limit_exceeded"`) with `Retry-After`.
- **Browser Lifecycle Recycling & Scale-to-Zero:** `BrowserPool` recycles Chromium instances after `PDF_MAX_USES_PER_BROWSER` (`default: 50`) uses, and `PlaywrightPDFGenerator` closes idle pools after `PDF_IDLE_TIMEOUT` (`default: 600s`) and lazily restarts on the next render/PDF request.
- **Structured Access Logs & Unprivileged Port `:8000`:** `CustomJsonFormatter` emits top-level `method`, `path`, and `status_code` fields on `uvicorn.access` records; `Dockerfile` listens on unprivileged TCP `:8000` without privileged port `sysctls`.

---

## Completed Work

### Sprint 4 — Ethical Crawling & Stateless Safety (Completed 2026-11-01)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S4-be-1 | Configurable `USER_AGENT` & `RESPECT_ROBOTS_TXT` propagated to `HTTPClient` and Playwright contexts | `src/downloader/config.py`, `src/downloader/http_client.py`, `src/downloader/pdf_generator.py`, `src/downloader/content_converter.py` | — |
| S4-be-2 | `RobotsChecker` (`urllib.robotparser`) with bounded in-memory TTL/LRU cache default, optional Redis backend, SSRF validation, and fail-open sentinel caching | `src/downloader/crawling/robots.py`, `src/downloader/crawling/__init__.py` | — |
| S4-be-3 | `DomainThrottle` per-domain `Crawl-delay` pacing across bounded in-memory locks and optional Redis coordination | `src/downloader/crawling/throttle.py` | — |
| S4-be-4 | Wire `robots.txt` & `Crawl-delay` pre-flight into `GET /{url}` and `POST /batch` with `RESPECT_ROBOTS_TXT=false` opt-out | `src/downloader/main.py`, `src/downloader/dependencies.py`, `src/downloader/routes/download.py`, `src/downloader/routes/batch.py` | — |
| S4-be-5 | Return `HTTP 403` (`robots_disallowed`) on `GET /{url}` and per-URL `403` in `/batch` results + Prometheus `robots_*` counters | `src/downloader/routes/download.py`, `src/downloader/routes/batch.py`, `src/downloader/metrics.py` | — |
| S4-test-1 | Comprehensive offline unit & mocked API suites for `RobotsChecker`, `DomainThrottle`, ethical crawling routes, HTTP/SSRF/rate-limit hardening, and browser pool recycling | `tests/unit/test_robots.py`, `tests/api/test_ethical_crawling.py`, `tests/unit/test_http_hardening.py`, `tests/unit/test_browser_recycling.py`, `tests/conftest.py` | — |
| S4-doc-1 | Document `v0.6.0` ethical crawling, hardening, and wire contracts; consolidate backlog to Roadmap-first layout | `README.md`, `AGENTS.md`, `CRITICAL.md`, `DECISIONS.md`, `.env.example`, `docs/roadmap.md`, `docs/guides/configuration.md`, `docs/guides/deployment.md`, `spec/contracts.md`, `skills/downloader-ops/SKILL.md` | — |

---

## Summary Statistics

| Category | Tasks Completed |
|----------|-----------------|
| Backend & Hardening | 5 |
| Testing | 1 |
| Documentation & Contracts | 1 |
| **Total** | **7** |

---

## Acceptance Criteria Met

- [x] `GET /{url}` and `POST /batch` evaluate `robots.txt` and `Crawl-delay` with zero Redis dependency when `REDIS_URI` is unset
- [x] Disallowed URLs return `HTTP 403` (`error_type: "robots_disallowed"`) naming the disallow rule and `User-Agent`
- [x] Unreachable or malformed `robots.txt` fails open with structured warning logs and cached fail-open sentinels
- [x] Bounded in-memory TTL/LRU cache and optional Redis backend pass identical offline contract tests
- [x] Streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`413`), redirect & Playwright SSRF route guards, active `RateLimitMiddleware` (`429` + `Retry-After`), browser recycling/scale-to-zero, and unprivileged `:8000` container verified
- [x] Documentation and `spec/contracts.md` updated for `v0.6.0`; sprint backlog consolidated into `docs/roadmap.md` + `DECISIONS.md`

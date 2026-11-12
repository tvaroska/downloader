# REST API Downloader - Strategic Roadmap

**Last Updated:** 2026-11-12

## Overview

This document outlines the strategic priorities, completed releases, and deferred backlog for the REST API Downloader service. Milestones are aligned with stateless-first Kubernetes operations (`REDIS_URI` optional for single-URL extraction, PDF rendering, rate limiting, and ethical crawling; required only for `/batch` and `/schedules`). Architectural rationale lives in [DECISIONS.md](../DECISIONS.md) and public wire contracts live in [spec/contracts.md](../spec/contracts.md).

---

## Current Status

| Phase | Status | Completion |
|-------|--------|------------|
| Phase 1: Core Functionality | ✅ Complete | 100% |
| Phase 2: Batch Processing | ✅ Complete | 100% |
| Phase 3: Performance & Reliability | ✅ Complete | 100% |
| Phase 4: Security & Production | ✅ Complete | 100% |
| Phase 5: Content Transformation | ✅ Complete (`v0.7.0` — OCR delivered) | 100% |
| Phase 6: Browser Rendering | ✅ Complete (`v0.7.0` — Screenshots, actions & `v0.6.2` timeout fix) | 100% |
| Phase 7: Ethical Crawling & Stateless Safety | ✅ Complete (`v0.6.0` — Sprint 4) | 100% |
| Phase 8: Scheduling & Multi-Tenant Quotas | 🚧 Partial (Scheduling & Webhooks ✅; Quotas deferred past `v0.7.0`) | 60% |

---

## Current Focus

### Completed: Release `v0.6.2` — Playwright Navigation Timeout Resilience (`F-2026-11-05-002`) ✅

**Release `v0.6.2`** (Complete — 2026-11-12 — P1 Reliability):
- [x] **S6-br-1 (P1 — Single-Pass `domcontentloaded` + Bounded Network Settle in `src/downloader/content_converter.py`):** Replaced the two-pass `networkidle` retry pattern in `convert_content_with_playwright_fallback` and `render_html_with_playwright` with a single `page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)` followed by a bounded non-fatal settle (`page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 2500))`), eliminating `10s–20s` navigation timeouts on ad- and tracker-heavy origins (`F-2026-11-05-002`), verified by `tests/unit/test_v062_playwright_timeout.py`.

### Completed: Release `v0.7.0` — Extended Features & Operations ✅

**Release `v0.7.0`** (Complete — 2026-11-12 — Extended Features & Operations/Infrastructure):
- **Extended Features (5/5 Complete):**
  - Screenshot/PDF output toggle (`?output=screenshot|pdf`, `?full_page=bool`, `?screenshot_format=png|jpeg`, `Accept: image/png` / `image/jpeg`, `POST /render`, and `/batch` + `/schedules` `"screenshot"` format)
  - SSRF-safe HMAC-SHA256 webhook notifications (`src/downloader/services/webhooks.py`) firing post-persistence on batch completion/failure and schedule execution (`X-Downloader-Signature-256`, `trust_env=False`, `follow_redirects=False`, creation-time SSRF validation, secret redaction)
  - Image-to-text OCR (`src/downloader/transformers/ocr.py` via `pytesseract` + `Pillow` + `tesseract-ocr`, decompression-bomb pixel cap, language validation, `HTTP 503` `ocr_unavailable` graceful degradation)
  - Typed client SDKs for Python (`sdks/python`), JavaScript/TypeScript (`sdks/javascript`), and Go (`sdks/go`) with automated test suites
  - Declarative allowlisted browser interaction scripting (`src/downloader/browser/actions.py`: 8 action types, max 20 steps, zero raw JS evaluation) across `GET /{url}?actions=`, `POST /render`, and `POST /batch`
- **Operations & Infrastructure (3/3 Complete):**
  - Graceful shutdown with active-request and background-job connection drain (`src/downloader/drain.py`: `DrainCoordinator`, `DrainMiddleware`, `HTTP 503 service_draining` + `Connection: close` + `Retry-After: 5`, `/health` draining state, uvicorn `--timeout-graceful-shutdown 35`)
  - Split Worker Architecture (`DEPLOYMENT_MODE=all|api|worker`, `src/downloader/worker_queue.py` reliable Redis `BLMOVE` queue + browser RPC + heartbeats, `src/downloader/worker.py` with `--healthcheck`, `docker-compose.split.yml`)
  - Opt-in OpenTelemetry distributed tracing (`src/downloader/telemetry.py`, `OTEL_ENABLED=false` default, OTLP HTTP/console exporters, FastAPI/httpx/Redis/Playwright/OCR/webhook/worker spans, `trace_id`/`span_id` log correlation, `X-Trace-ID` response header, W3C `traceparent` propagation)

### Completed: Release `v0.6.1` — Security, Single-Fetch Browser & Stateful Pipeline Hardening ✅

**Release `v0.6.1`** (Complete — 2026-11-02): Defense-in-Depth SSRF Pinning, Single-Fetch Playwright Pipeline & Stateful Parity
- **Track A (Browser & Single-Fetch Pipeline):** Eliminated redundant pre-flight HTTP fetches on `Accept: application/pdf` and `?render=true` / `?wait_for=` across `text/html`, `text/plain`, `text/markdown`, and multi-format requests; unified all Chromium pathways under `pdf_semaphore` (`503` fail-fast on explicit `?render=true`/PDF, graceful static degradation on auto-detect/empty-body fallback); removed the unconditional `2s` sleep in `generate_pdf`; wired `ContentConfig` flags and monotonic-TTL `BoundedCache`
- **Track B (Auth, SSRF Pinning & Rate Limits):** Restored `.env` loading in `AuthConfig` with `secrets.compare_digest` verification; required API key auth on `/metrics*` when `DOWNLOADER_KEY` is set and disabled `/docs`, `/redoc`, and `/openapi.json` (`HTTP 404`) in `ENVIRONMENT=production`; implemented `SSRFSafeNetworkBackend` / `SSRFSafeHTTPTransport` (`trust_env=False`, DNS-pinned `connect_tcp` preventing DNS-rebinding TOCTOU, non-blocking `asyncio.to_thread` DNS resolution, and hex/octal/int/mapped-IPv6/`*.localhost` blocks); added right-to-left `RATELIMIT_TRUSTED_PROXIES` `X-Forwarded-For` client IP resolution and auth vs unauth bucket isolation
- **Track C & D (Stateful `/batch`, `/schedules` & Redis Atomicity):** Added creation-time SSRF & custom header sanitization (`HTTP 400`) on `POST /schedules` and `POST /batch`, `robots.txt` + `DomainThrottle` + non-retryable policy short-circuit + per-attempt `SCHEDULER_MAX_WORKERS` semaphore + JSON `content_size` accounting in `ScheduledJobExecutor`, atomic Lua `DomainThrottle` Redis slot reservation, `(scheme, netloc)` `robots:v2:{scheme}:{netloc}` cache isolation, and incremental `/batch` progress updates

### Completed: Sprint 4 — Ethical Crawling & Production Hardening (`v0.6.0`) ✅

**Sprint 4** (Complete — 2026-11-01): Ethical Crawling, Egress Protection & Defense-in-Depth Hardening
- 7 tasks completed (`S4-be-1`..`S4-be-5`, `S4-test-1`, `S4-doc-1`)
- Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`) across `HTTPClient` and Playwright
- Stateless-first `robots.txt` evaluation (`urllib.robotparser`) with bounded in-memory TTL/LRU cache by default (TTL 1h, zero Redis dependency), optional Redis backend when `REDIS_URI` is configured, and cached fail-open sentinels on unreachable origins
- Per-domain `Crawl-delay` throttling (in-memory timestamps by default + optional Redis coordination), `HTTP 403` `robots_disallowed` responses, and Prometheus `robots_*` counters
- Production hardening: streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413` `content_too_large`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After` + `X-RateLimit-*`), `BrowserPool` max-use recycling (`PDF_MAX_USES_PER_BROWSER=50`) & idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`), structured JSON `uvicorn.access` `path`/`method`/`status_code` fields, and unprivileged container port `:8000`
- See [docs/features/ethical-crawling.md](features/ethical-crawling.md)

### Deferred Preview (Post-`v0.7.0` / `v0.8.0+`): Multi-Tenant Quotas

**Post-`v0.7.0` Backlog** (Deferred until multi-key or Redis-backed multi-tenant deployment): Multi-Tenant Per-Key Quotas
- 7 tasks (`S5-be-1`..`S5-be-5`, `S5-test-1`, `S5-doc-1`), ~21 hours effort
- `QuotaConfig`, per-key request/bandwidth counters, `X-RateLimit-{Limit,Remaining,Reset}` headers, `GET /usage`, and `HTTP 429` with `Retry-After`
- Deferred past `v0.7.0` because single-key stateless Kubernetes deployments without `REDIS_URI` do not need Redis-backed tenant accounting (`slowapi` `memory://` already enforces global endpoint rate limits); if activated later, quota storage should support `QUOTA_STORAGE_URI → REDIS_URI → memory://` parity

### Completed: Sprint 3 — Scheduling API ✅

**Sprint 3** (Complete): Scheduling CRUD API
- 7 tasks completed
- Cron-based scheduling, job execution with retry, paginated execution history
- See [docs/features/scheduling.md](features/scheduling.md)

---

## Priority Areas

### ✅ P0: Production Readiness (Phase 4 — Complete)

**Completed:** 2026-01-18  
**Archive:** [docs/features/production-readiness.md](features/production-readiness.md)

CI/CD pipeline, offline smoke/unit/hygiene verification gate (`tests/smoke/test_repo_hygiene.py` + deterministic fixture DNS), container hardening, structured logging, and documentation alignment.

---

### ✅ P1: Ethical Crawling & Stateless Safety (Phase 7 — Complete)

**Completed:** 2026-11-01  
**Archive:** [docs/features/ethical-crawling.md](features/ethical-crawling.md)

**Goal**: Prevent egress abuse and IP bans via responsible `robots.txt` compliance and `Crawl-delay` pacing that work out-of-the-box in stateless single-key deployments as well as Redis-backed clusters.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Configurable `USER_AGENT` & `RESPECT_ROBOTS_TXT` | P1 | Low | ✅ Complete (`S4-be-1`) |
| `robots.txt` parser + bounded in-memory TTL/LRU cache (optional Redis) | P1 | Medium | ✅ Complete (`S4-be-2`) |
| Per-domain `Crawl-delay` throttling (in-memory + optional Redis) | P1 | Medium | ✅ Complete (`S4-be-3`) |
| `GET /{url}` & `POST /batch` pre-flight enforcement | P1 | Medium | ✅ Complete (`S4-be-4`) |
| Structured `HTTP 403` disallow responses & metrics | P1 | Low | ✅ Complete (`S4-be-5`) |

#### Delivered Configuration & Wire Behavior
- Settings in `src/downloader/config.py` (`CrawlingConfig`): `RESPECT_ROBOTS_TXT` (`default: True`), `USER_AGENT` (`default: REST-API-Downloader/1.0`), `ROBOTS_CACHE_TTL` (`default: 3600`), `ROBOTS_CACHE_MAX_SIZE` (`default: 1000`), `ROBOTS_FETCH_TIMEOUT` (`default: 5.0`), `MAX_CRAWL_DELAY` (`default: 30.0`)
- Cache backend chain: bounded in-memory TTL/LRU cache (TTL 1h) when `REDIS_URI` is unset; Redis shared cache (`robots:v1:{netloc}`) when `REDIS_URI` is configured
- Fail-open with structured warning log + cached sentinel when `robots.txt` is unreachable; returns `HTTP 403` (`error_type: "robots_disallowed"`) when blocked

#### Success Criteria Met
- ✅ `robots.txt` and `Crawl-delay` enforced before single and batch downloads without requiring Redis
- ✅ Caching eliminates repeat `robots.txt` fetches within TTL across memory and Redis backends
- ✅ 100% offline-sandbox-safe unit and mocked API test coverage (`tests/unit/test_robots.py`, `tests/api/test_ethical_crawling.py`)

**Core Archive:** [docs/features/downloader-core.md](features/downloader-core.md)

---

### ✅ P1: Content Transformation (Phase 5 — Complete)

**Goal**: Transform downloaded content into formats optimized for downstream consumption (LLMs, data pipelines).

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| HTML to Markdown | P1 | Medium | ✅ Complete |
| Plain text extraction | P2 | Low | ✅ Complete |
| `Accept` content negotiation (single raw vs multi-format JSON) | P1 | Medium | ✅ Complete |
| Image to Text (OCR) | P2 | High | ✅ Complete (`v0.7.0`) |

#### API Implemented
- `Accept` header negotiation on `GET /{url}`: raw payload for single MIME type (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, `application/json`) and JSON map for multi-format `Accept` headers
- OCR image-to-text (`?ocr=true`, `?ocr_lang=eng`, automatic extraction on image origins for `text/plain` / `text/markdown` / `text/html`)
- Batch support: `format` (`text`, `markdown`, `html`, `pdf`, `screenshot`, `json`, `raw`) and `default_format` options in request body

**Archive:** [docs/features/content-transformation.md](features/content-transformation.md)

---

### ✅ P2: Browser Rendering (Phase 6 — Complete)

**Goal**: Capture JavaScript-rendered content, screenshots, and PDFs from SPAs and dynamic websites without penalizing non-browser startup or mocked test latency.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Basic JS rendering (`?render=true`) | P1 | High | ✅ Complete |
| Wait for CSS selectors (`?wait_for=`) | P1 | Medium | ✅ Complete |
| Lazy shared `BrowserPool` & process isolation | P1 | Medium | ✅ Complete |
| Security hardening (`file:` scheme blocked, isolated contexts) | P1 | Medium | ✅ Complete |
| Single-pass `domcontentloaded` + bounded settle (`S6-br-1`) | P1 | Low | ✅ Complete (`v0.6.2`) |
| Screenshot/PDF output toggle (`?output=screenshot\|pdf`) | P2 | Medium | ✅ Complete (`v0.7.0`) |
| Interaction scripting (`POST /render`, `?actions=`) | P3 | High | ✅ Complete (`v0.7.0`) |

**Archive:** [docs/features/browser-rendering.md](features/browser-rendering.md)

---

### 🚧 P3: Scheduling & Multi-Tenant Quotas (Phase 8 — Scheduling & Webhooks Complete, Quotas Deferred)

**Goal**: Support recurring downloads with webhook notifications (when Redis is deployed) and defer per-tenant quota accounting until multi-key or Redis-backed tenancy is required.

#### Scheduling Features (Sprint 3 + `v0.7.0` — Complete)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Cron-based scheduling | P1 | Medium | ✅ Complete |
| Schedule CRUD API (`/schedules`) | P1 | Low | ✅ Complete |
| Paginated execution history (`/schedules/{id}/history`) | P2 | Low | ✅ Complete |
| Webhook on completion | P2 | Medium | ✅ Complete (`v0.7.0`) |

#### Multi-Tenant Quota Features (Post-`v0.7.0` / `v0.8.0+` — Deferred Preview)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| `QuotaConfig` & per-API-key counters (`memory://` + Redis parity) | P2 | Medium | 📋 Post-`v0.7.0` (Deferred) |
| Enforcement middleware (`X-RateLimit-{Limit,Remaining,Reset}`) | P2 | Medium | 📋 Post-`v0.7.0` (Deferred) |
| Usage tracking endpoint (`GET /usage`) | P2 | Low | 📋 Post-`v0.7.0` (Deferred) |
| `HTTP 429` with `Retry-After` header | P2 | Low | 📋 Post-`v0.7.0` (Deferred) |
| Tier-based plans | P3 | Medium | 📋 Post-`v0.7.0` (Deferred) |

**Scheduling Archive:** [docs/features/scheduling.md](features/scheduling.md)

---

## Release `v0.7.0` Scope (Extended Features & Operations — Complete)

### Extended Features (`v0.7.0`)

| Feature | Description | Complexity | Priority | Status |
|---------|-------------|------------|----------|--------|
| Screenshot/PDF output | `?output=screenshot\|pdf`, `?full_page=`, `?screenshot_format=`, `Accept: image/png\|jpeg` | Medium | P2 | ✅ Complete (`v0.7.0`) |
| Webhook notifications | SSRF-safe HMAC-SHA256 webhooks on batch/schedule completion | Medium | P2 | ✅ Complete (`v0.7.0`) |
| OCR (Image to Text) | Pytesseract + Pillow image-to-text extraction with decompression-bomb guard | High | P2 | ✅ Complete (`v0.7.0`) |
| SDK/Client Libraries | Typed Python, JavaScript/TypeScript, and Go clients (`sdks/`) | Medium | P3 | ✅ Complete (`v0.7.0`) |
| Interaction scripting | Declarative allowlisted Playwright actions (`?actions=`, `POST /render`, `/batch`) | High | P3 | ✅ Complete (`v0.7.0`) |

### Operations & Infrastructure (`v0.7.0`)

| Feature | Description | Complexity | Priority | Status |
|---------|-------------|------------|----------|--------|
| Prometheus & health-score metrics | `/metrics`, `/metrics/performance`, `/metrics/health-score`, `/metrics/live` | Low | P1 | ✅ Complete |
| Graceful shutdown with connection drain | Zero-downtime pod termination draining active downloads (`drain.py`) | Low | P2 | ✅ Complete (`v0.7.0`) |
| Split Worker Architecture | `DEPLOYMENT_MODE=all\|api\|worker` with Redis `BLMOVE` queue & browser RPC | High | P2 | ✅ Complete (`v0.7.0`) |
| OpenTelemetry integration | Opt-in distributed tracing (`OTEL_ENABLED`) across API, HTTP, Redis, Playwright, OCR, webhooks | Medium | P2 | ✅ Complete (`v0.7.0`) |

**Infrastructure Archive:** [docs/features/infrastructure.md](features/infrastructure.md)

---

## Technical Dependencies

### Content Transformation (Complete — `v0.7.0`)
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0
pytesseract>=0.3.13
Pillow>=11.0.0
# System: tesseract-ocr, tesseract-ocr-eng
```

### Browser Rendering (Complete — Lazy Shared Pool)
```
playwright>=1.40.0
# System: playwright install chromium
```

### Scheduling & Split Worker (Complete — Requires `REDIS_URI`)
```
apscheduler>=3.10.0
redis>=5.0.0
```

### Observability (Opt-In — `OTEL_ENABLED=true`)
```
opentelemetry-api>=1.27.0
opentelemetry-sdk>=1.27.0
opentelemetry-exporter-otlp-proto-http>=1.27.0
opentelemetry-instrumentation-fastapi>=0.48b0
opentelemetry-instrumentation-httpx>=0.48b0
opentelemetry-instrumentation-redis>=0.48b0
```

### Sprint 4: Ethical Crawling (Stateless-First)
```
# Python stdlib urllib.robotparser + bounded in-memory TTL/LRU cache (zero new mandatory deps)
# Optional shared cache/coordination via existing redis>=5.0.0 when REDIS_URI is set
```

---

## Risk Matrix

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Browser rendering OOM or cold-start overhead | High | Low | Lazy `BrowserPool` init, isolated contexts, memory/concurrency semaphores ✅ |
| `robots.txt` origin unreachable or slow | Medium | Medium | Short fetch timeout + fail-open with structured warning log + 1h TTL cache |
| Stateless multi-pod cache divergence without Redis | Low | Medium | Per-pod bounded TTL/LRU cache is safe for `robots.txt`; optional `REDIS_URI` unifies across pods |
| Scheduler job failures | Medium | Low | Retry policies + clear `503` when `REDIS_URI` is unset ✅ |
| Quota bypass in future multi-tenant mode | Medium | Low | Server-side middleware enforcement when post-`v0.7.0` multi-tenant quotas activate |

---

## Decision Log

Canonical append-only rationale lives in [DECISIONS.md](../DECISIONS.md). Summary of roadmap milestones:

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-11-12 | Implement `v0.6.2` (`S6-br-1` single-pass Playwright settle) and full `v0.7.0` scope (all 5 Extended Features + all 3 Operations tracks) | Eliminated `10s–20s` tracker-heavy navigation timeouts and shipped screenshot/PDF toggles, allowlisted Playwright actions (`POST /render`), SSRF-safe HMAC webhooks, Tesseract OCR, typed Python/JS/Go SDKs, graceful connection drain, split API+worker Redis architecture, and opt-in OpenTelemetry tracing while preserving stateless single-pod defaults |
| 2026-11-12 | Replan `v0.7.0` around Extended Features & Operations; move Multi-Tenant Quotas past `v0.7.0` (`v0.8.0+`) | Single-key GKE production gets higher immediate value from extraction features (screenshots, webhooks, OCR, SDKs, interaction scripting) and operational resilience (graceful drain, split workers, OTel) than per-tenant quotas |
| 2026-11-02 | Release `v0.6.1` (DNS-pinned SSRF transport, single-fetch Playwright pipeline, `/metrics*` auth & prod OpenAPI disablement, and `/batch` + `/schedules` hardening) | Closed DNS-rebinding TOCTOU, XFF spoofing, unauthenticated `/metrics*` exposure, duplicate HTTP+Chromium fetches, and stateful `/batch` + `/schedules` configuration/crawling gaps while preserving stateless GKE defaults |
| 2026-11-01 | Release `v0.6.0` (Sprint 4 Ethical Crawling + production hardening) & consolidate to Roadmap-first layout | Delivered stateless-first `robots.txt`/`Crawl-delay`, streaming `413`, redirect & Playwright SSRF guards, active `429` rate limits, browser recycling/scale-to-zero, `:8000` unprivileged port, and archived Sprint 4 into `docs/features/ethical-crawling.md` |
| 2026-11-01 | Promote Ethical Crawling (stateless-first) to Sprint 4; defer Multi-Tenant Quotas to Sprint 5 | Production runs statelessly behind a single API key without Redis; ethical crawling protects live egress immediately via in-memory TTL cache + optional Redis |
| 2026-11-01 | Lazy `PlaywrightPDFGenerator` pool + offline verification gate | Eliminates Chromium startup cost on non-browser paths and guarantees deterministic offline smoke/unit hygiene checks |
| 2026-08-20 | Adopt structured `spec/` + `DECISIONS.md` + `docs/features/` archive layout | Separate status-free wire contracts (`spec/`) from roadmap milestones (`docs/roadmap.md`) and decision rationale (`DECISIONS.md`) |
| 2026-01-21 | Sprint 3 (Scheduling API) complete | Cron scheduling, retries, and execution history delivered |
| 2026-01-19 | Sprint 2 (Browser Rendering) complete | Playwright rendering, selector waits, and security isolation delivered |
| 2026-01-18 | Sprint 1 (Content Transformation) & Sprint 0 (Production Readiness) complete | Markdown/plain-text extraction, `Accept` negotiation, and SSRF hardening delivered |

---

## Release Plan

### Release 0.3.0 — Content Transformation (Complete)
- ✅ HTML to Markdown conversion, plain text extraction, and `Accept` content negotiation

### Release 0.4.0 — Browser Rendering (Complete)
- ✅ Playwright JS rendering (`?render=true`, `?wait_for=`), process isolation, and lazy shared `BrowserPool`

### Release 0.5.0 — Scheduling & Stateless Hardening (Complete)
- ✅ Cron-based scheduling CRUD & history API (`REDIS_URI` mode), explicit `503` stateless guards, and offline repo hygiene gate

### Release 0.6.0 — Ethical Crawling & Production Hardening (Complete)
- ✅ Stateless-first `robots.txt` compliance (`urllib.robotparser`, bounded in-memory TTL/LRU cache default + optional Redis)
- ✅ Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`)
- ✅ Per-domain `Crawl-delay` throttling, structured `HTTP 403` (`robots_disallowed`) responses, and Prometheus `robots_*` metrics
- ✅ Streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After`), browser max-use recycling & idle scale-to-zero, JSON `uvicorn.access` `path` fields, and unprivileged container port `:8000`

### Release 0.6.1 — Security, Single-Fetch Browser & Stateful Pipeline Hardening (Complete)
- ✅ DNS-pinned `SSRFSafeHTTPTransport` (`trust_env=False`), async DNS resolution, and hex/octal/int/mapped-IPv6/`*.localhost` SSRF blocks
- ✅ Single-fetch Playwright pipeline across `?render=true`, `?wait_for=`, and `Accept: application/pdf` (`GET /{url}`, `/batch`, `/schedules`), unified `pdf_semaphore` concurrency, and live `ContentConfig` / TTL cache wiring
- ✅ Constant-time API key verification, `/metrics*` auth enforcement, `ENVIRONMENT=production` `404` on `/docs`/`/redoc`/`/openapi.json`, right-to-left `RATELIMIT_TRUSTED_PROXIES` `X-Forwarded-For` parsing, and auth/unauth rate-limit isolation
- ✅ Creation-time SSRF & custom header validation on `/schedules` and `/batch`, `ScheduledJobExecutor` `robots.txt` + `Crawl-delay` + per-attempt `max_workers` semaphore, atomic Redis Lua `DomainThrottle`, and `robots:v2:{scheme}:{netloc}` cache keys

### Release 0.6.2 — Playwright Navigation Timeout Resilience (Complete — `F-2026-11-05-002`)
- ✅ Single-pass `wait_until="domcontentloaded"` + bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`) across `convert_content_with_playwright_fallback` and `render_html_with_playwright` in `src/downloader/content_converter.py`, eliminating `10s–20s` timeouts on Substack/ad-heavy pages (`materializedview.io`, `novinky.cz`)

### Release 0.7.0 — Extended Features & Operations (Complete)
- ✅ **Extended Features:** Screenshot/PDF output toggle (`?output=screenshot|pdf`, `?full_page=`, `?screenshot_format=`, `Accept: image/png|jpeg`), SSRF-safe HMAC-SHA256 webhook notifications on batch/schedule completion, OCR (image-to-text via `pytesseract` + `Pillow` + `tesseract-ocr`), typed client SDKs (`sdks/python`, `sdks/javascript`, `sdks/go`), and declarative allowlisted browser interaction scripting (`?actions=`, `POST /render`, `POST /batch`)
- ✅ **Operations & Infrastructure:** Graceful shutdown with active-download connection drain (`DrainCoordinator` + `DrainMiddleware`), split worker architecture (`DEPLOYMENT_MODE=all|api|worker`, Redis `BLMOVE` queue + remote browser RPC + `docker-compose.split.yml`), and opt-in OpenTelemetry distributed tracing (`OTEL_ENABLED=false` default, OTLP HTTP/console exporters, log correlation, W3C `traceparent` propagation)

### Release 0.8.0+ — Multi-Tenant Quotas (Deferred — Post-`0.7.0`)
- Per-API-key `QuotaConfig`, `GET /usage`, `X-RateLimit-*` headers, `HTTP 429` `Retry-After`, and tier-based plans (`QUOTA_STORAGE_URI → REDIS_URI → memory://` parity when multi-key tenancy is needed)
- Deferred past `v0.7.0` until multi-key or Redis-backed multi-tenant deployment is required

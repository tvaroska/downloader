# REST API Downloader - Strategic Roadmap

**Last Updated:** 2026-11-01

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
| Phase 5: Content Transformation | 🚧 Partial (OCR deferred) | 75% |
| Phase 6: Browser Rendering | 🚧 Partial (Screenshots deferred) | 75% |
| Phase 7: Ethical Crawling & Stateless Safety | ✅ Complete (`v0.6.0` — Sprint 4) | 100% |
| Phase 8: Scheduling & Multi-Tenant Quotas | 🚧 Partial (Scheduling ✅; Quotas deferred to Sprint 5) | 50% |

---

## Current Focus

### Completed: Sprint 4 — Ethical Crawling & Production Hardening (`v0.6.0`) ✅

**Sprint 4** (Complete — 2026-11-01): Ethical Crawling, Egress Protection & Defense-in-Depth Hardening
- 7 tasks completed (`S4-be-1`..`S4-be-5`, `S4-test-1`, `S4-doc-1`)
- Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`) across `HTTPClient` and Playwright
- Stateless-first `robots.txt` evaluation (`urllib.robotparser`) with bounded in-memory TTL/LRU cache by default (TTL 1h, zero Redis dependency), optional Redis backend when `REDIS_URI` is configured, and cached fail-open sentinels on unreachable origins
- Per-domain `Crawl-delay` throttling (in-memory timestamps by default + optional Redis coordination), `HTTP 403` `robots_disallowed` responses, and Prometheus `robots_*` counters
- Production hardening: streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413` `content_too_large`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After` + `X-RateLimit-*`), `BrowserPool` max-use recycling (`PDF_MAX_USES_PER_BROWSER=50`) & idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`), structured JSON `uvicorn.access` `path`/`method`/`status_code` fields, and unprivileged container port `:8000`
- See [docs/features/ethical-crawling.md](features/ethical-crawling.md)

### Deferred Preview: Sprint 5 — Multi-Tenant Quotas

**Sprint 5** (Deferred until multi-key or Redis-backed deployment): Multi-Tenant Per-Key Quotas
- 7 tasks (`S5-be-1`..`S5-be-5`, `S5-test-1`, `S5-doc-1`), ~21 hours effort
- `QuotaConfig`, per-key request/bandwidth counters, `X-RateLimit-{Limit,Remaining,Reset}` headers, `GET /usage`, and `HTTP 429` with `Retry-After`
- Deferred because single-key stateless Kubernetes deployments without `REDIS_URI` do not need Redis-backed tenant accounting (`slowapi` `memory://` already enforces global endpoint rate limits); if activated later, quota storage should support `QUOTA_STORAGE_URI → REDIS_URI → memory://` parity

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

### 🚧 P1: Content Transformation (Phase 5 — Partial)

**Goal**: Transform downloaded content into formats optimized for downstream consumption (LLMs, data pipelines).

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| HTML to Markdown | P1 | Medium | ✅ Complete |
| Plain text extraction | P2 | Low | ✅ Complete |
| `Accept` content negotiation (single raw vs multi-format JSON) | P1 | Medium | ✅ Complete |
| Image to Text (OCR) | P2 | High | 📋 Sprint 6 Backlog |

#### API Implemented
- `Accept` header negotiation on `GET /{url}`: raw payload for single MIME type (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `application/json`) and JSON map for multi-format `Accept` headers
- Batch support: `format` and `default_format` options in request body

**Archive:** [docs/features/content-transformation.md](features/content-transformation.md)

---

### 🚧 P2: Browser Rendering (Phase 6 — Partial)

**Goal**: Capture JavaScript-rendered content and PDFs from SPAs and dynamic websites without penalizing non-browser startup or mocked test latency.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Basic JS rendering (`?render=true`) | P1 | High | ✅ Complete |
| Wait for CSS selectors (`?wait_for=`) | P1 | Medium | ✅ Complete |
| Lazy shared `BrowserPool` & process isolation | P1 | Medium | ✅ Complete |
| Security hardening (`file:` scheme blocked, isolated contexts) | P1 | Medium | ✅ Complete |
| Screenshot/PDF output toggle (`?output=screenshot\|pdf`) | P2 | Medium | 📋 Sprint 6 Backlog |
| Interaction scripting | P3 | High | 📋 Backlog |

**Archive:** [docs/features/browser-rendering.md](features/browser-rendering.md)

---

### 🚧 P3: Scheduling & Multi-Tenant Quotas (Phase 8 — Scheduling Complete, Quotas Deferred)

**Goal**: Support recurring downloads (when Redis is deployed) and defer per-tenant quota accounting until multi-key or Redis-backed tenancy is required.

#### Scheduling Features (Sprint 3 — Complete)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Cron-based scheduling | P1 | Medium | ✅ Complete |
| Schedule CRUD API (`/schedules`) | P1 | Low | ✅ Complete |
| Paginated execution history (`/schedules/{id}/history`) | P2 | Low | ✅ Complete |
| Webhook on completion | P3 | Medium | 📋 Sprint 6 Backlog |

#### Multi-Tenant Quota Features (Sprint 5 — Deferred Preview)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| `QuotaConfig` & per-API-key counters (`memory://` + Redis parity) | P2 | Medium | 📋 Sprint 5 (Deferred) |
| Enforcement middleware (`X-RateLimit-{Limit,Remaining,Reset}`) | P2 | Medium | 📋 Sprint 5 (Deferred) |
| Usage tracking endpoint (`GET /usage`) | P2 | Low | 📋 Sprint 5 (Deferred) |
| `HTTP 429` with `Retry-After` header | P2 | Low | 📋 Sprint 5 (Deferred) |
| Tier-based plans | P3 | Medium | 📋 Backlog |

**Scheduling Archive:** [docs/features/scheduling.md](features/scheduling.md)

---

## Future Considerations (Sprint 6 & Backlog)

### Extended Features (Sprint 6 Candidates)

| Feature | Description | Complexity | Priority |
|---------|-------------|------------|----------|
| Screenshot/PDF output | `?output=screenshot\|pdf` for browser captures | Medium | P2 |
| Webhook notifications | Push batch/schedule results to configured endpoints | Medium | P2 |
| OCR (Image to Text) | Pytesseract-based text extraction | High | P2 |
| SDK/Client Libraries | Typed Python, JavaScript, and Go clients | Medium | P3 |

### Operations & Infrastructure

| Feature | Description | Complexity | Priority | Status |
|---------|-------------|------------|----------|--------|
| Prometheus & health-score metrics | `/metrics`, `/metrics/performance`, `/metrics/health-score`, `/metrics/live` | Low | P1 | ✅ Complete |
| Graceful shutdown with connection drain | Zero-downtime pod termination draining active downloads | Low | P2 | 📋 Backlog |
| Split Worker Architecture | Isolate stateless API pods from Playwright/scheduler workers via Redis queue | High | P2 | 📋 Backlog |
| OpenTelemetry integration | Distributed tracing across API, HTTP client, Playwright, and optional Redis | Medium | P2 | 📋 Backlog |

**Infrastructure Archive:** [docs/features/infrastructure.md](features/infrastructure.md)

---

## Technical Dependencies

### Content Transformation (Complete; OCR Deferred to Sprint 6)
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0
# Deferred Sprint 6 OCR: pytesseract>=0.3.10, Pillow>=10.0.0 + system tesseract-ocr
```

### Browser Rendering (Complete — Lazy Shared Pool)
```
playwright>=1.40.0
# System: playwright install chromium
```

### Scheduling (Complete — Requires `REDIS_URI`)
```
apscheduler>=3.10.0
redis>=5.0.0
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
| Quota bypass in future multi-tenant mode | Medium | Low | Server-side middleware enforcement when Sprint 5 activates |

---

## Decision Log

Canonical append-only rationale lives in [DECISIONS.md](../DECISIONS.md). Summary of roadmap milestones:

| Date | Decision | Rationale |
|------|----------|-----------|
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

### Release 0.7.0 — Multi-Tenant Quotas (Deferred — Sprint 5)
- Per-API-key `QuotaConfig`, `GET /usage`, `X-RateLimit-*` headers, and `HTTP 429` `Retry-After` (with `memory://` + Redis parity when multi-key tenancy is needed)
- Target: Sprint 5 completion

### Release 0.8.0 — Extended Features (Future — Sprint 6)
- Screenshot/PDF output toggle, webhook notifications, OCR (Image to Text), and client SDKs
- Target: Sprint 6 completion

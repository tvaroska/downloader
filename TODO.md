# REST API Downloader — TODO

**Goal:** A high-performance, safe URL-download service — content negotiation,
batch jobs, scheduling, and per-key quotas for multi-tenant deployments.
**Updated:** 2026-08-20

<!-- Counters: doc=5 be=5 test=1 -->

Live status lives ONLY here. States: `- [ ]` open · `- [x]` done · `- [!]`
attempted-but-failed. `spec/` is status-free (the WHAT); `docs/` is planning/ops.

---

## Sprint 0 — Triage (always active)

_No open critical issues._

Recently resolved (usability fixes — substance archived in `DECISIONS.md`):
- [x] **S0-doc-1** Document schedule CRUD endpoints in `README.md` + `spec/contracts.md`.
- [x] **S0-doc-2** Document metrics endpoints (`/metrics`, `/metrics/performance`,
  `/metrics/health-score`) in `README.md` + `spec/contracts.md`.
- [x] **S0-doc-3** Document implicit Playwright fallback in batch processing (`spec/contracts.md`).
- [x] **S0-doc-4** Add Getting Started section — minimal (no Redis/Playwright) vs full setup (`README.md`).
- [x] **S0-doc-5** Document rate-limit storage fallback chain
  (`RATELIMIT_STORAGE_URI → REDIS_URI → memory://`) in `.env.example` + `docs/guides/configuration.md`.
- [x] **S0-be-1** Return clear 503 when Redis is unavailable on batch/schedule endpoints,
  naming the feature that needs Redis.
- [x] **S0-be-2** Fix ErrorResponse nesting inconsistency; OpenAPI schema now matches
  actual `detail` wrapping across routes.

---

## Sprint 4 — Quota Management (CURRENT)

Per-API-key quotas, usage tracking, and rate limiting for multi-tenant deployments.

### Quota Infrastructure (P1 — Core)
- [ ] **S4-be-1** Quota configuration model — `src/downloader/quota/models.py`
  (`QuotaConfig`): daily/monthly request limits + bandwidth limits; store in API-key
  metadata in Redis. Files: `src/downloader/quota/models.py`, `src/downloader/models/api_key.py`.
- [ ] **S4-be-2** Per-API-key request counting — `src/downloader/quota/counter.py`,
  Redis `INCR` + TTL for time-windowed counting (daily/monthly), atomic increment with quota check.
- [ ] **S4-be-3** Quota enforcement middleware — `src/downloader/quota/middleware.py`:
  check quota pre-request, inject `X-RateLimit-{Limit,Remaining,Reset}` headers,
  target < 10ms overhead. Files: `src/downloader/quota/middleware.py`, `src/downloader/main.py`.
- [ ] **S4-be-4** Usage tracking endpoint — `GET /usage`: requests used/remaining, reset
  time, bandwidth if configured. Files: `src/downloader/routes/usage.py`.
- [ ] **S4-be-5** 429 responses with `Retry-After` — return 429 when quota exceeded,
  seconds-until-reset header, helpful message linking `/usage`.

### Testing (P1 — Required)
- [ ] **S4-test-1** Quota unit + integration tests — model validation, counter
  increment/reset, middleware enforcement, `/usage` responses, 429 + `Retry-After`,
  concurrent requests. Files: `tests/unit/test_quota.py`, `tests/integration/test_quota.py`.

### Documentation (P1 — Required)
- [ ] **S4-doc-1** Document quota + usage endpoints — config in deployment guide,
  `/usage` in `spec/contracts.md`, rate-limit headers, troubleshooting.
  Files: `spec/contracts.md`, `docs/guides/deployment.md`.

**Acceptance criteria (T2 gate — see `spec/cujs.md` when defined):**
- `GET /usage` returns current quota status.
- 429 response includes `Retry-After` header.
- Quota enforcement has < 10ms overhead per request.
- Usage stats accurate within 1% margin.
- All quota tests pass.

---

## Sprint 5 Preview — Ethical Crawling

Respect `robots.txt`, configurable User-Agent — prevent IP bans, enable responsible crawling.

| Task ID | Description | Effort |
|---------|-------------|--------|
| S5-be-1 | Integrate `robots.txt` parser with Redis caching (TTL default 1h) | 3h |
| S5-be-2 | Add `RESPECT_ROBOTS_TXT` config flag (default: True) | 2h |
| S5-be-3 | Add `USER_AGENT` config flag with default value | 1h |
| S5-be-4 | Check `robots.txt` before every download request | 3h |
| S5-be-5 | Return 403 with reason when URL disallowed | 2h |
| S5-test-1 | Add `robots.txt` unit + integration tests | 3h |
| S5-doc-1 | Document ethical-crawling configuration | 2h |

Per-domain rate limiting from `Crawl-delay`; parse via stdlib `urllib.robotparser`.

---

## Sprint 6 Preview — Extended Features

Candidates (to be prioritized): screenshot/PDF output (`?output=screenshot|pdf`),
webhook notifications on job completion, OCR (image→text), SDK/client libraries.

---

## Completed Sprints

| Sprint | Theme | Archive |
|--------|-------|---------|
| S0 | Production Readiness | [docs/features/production-readiness.md](docs/features/production-readiness.md) |
| S1 | Content Transformation | [docs/features/content-transformation.md](docs/features/content-transformation.md) |
| S2 | Browser Rendering | [docs/features/browser-rendering.md](docs/features/browser-rendering.md) |
| S3 | Scheduling API | [docs/features/scheduling.md](docs/features/scheduling.md) |

Decisions log: `DECISIONS.md` · Requirements: `spec/` · Roadmap: `docs/roadmap.md`.

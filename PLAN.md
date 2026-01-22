# REST API Downloader - Sprint Plan

**Last Updated:** 2026-01-21

## Sprint 0 - Critical Remediation ✅ COMPLETE

**Status:** All 19 tasks completed
**Completed:** 2026-01-20
**Archive:** [docs/features/production-readiness.md](docs/features/production-readiness.md)

Summary: Test coverage increased to 85%+, all documentation fixes, CI/CD hardening with mypy and pip-audit, CORS security defaults, secrets detection.

---

## Current Sprint: Sprint 4 (Quota Management)

**Priority:** Medium - Usage limits for multi-tenant deployments
**Focus:** Per-API-key quotas, usage tracking, rate limiting

---

### 1. Quota Infrastructure (P1 - Core)

1.1. **S4-BE-1: Add quota configuration model**
   - [ ] Create `src/downloader/quota/models.py` with QuotaConfig Pydantic model
   - [ ] Support request limits (daily/monthly) and bandwidth limits
   - [ ] Add quota config to API key metadata in Redis
   - Files: `src/downloader/quota/models.py`, `src/downloader/models/api_key.py`
   - Effort: 2h

1.2. **S4-BE-2: Implement per-API-key request counting**
   - [ ] Create `src/downloader/quota/counter.py` with Redis-based counters
   - [ ] Use Redis INCR with TTL for time-windowed counting
   - [ ] Support daily and monthly quota periods
   - [ ] Add atomic increment with quota check
   - Files: `src/downloader/quota/counter.py`
   - Effort: 4h

1.3. **S4-BE-3: Add quota enforcement middleware**
   - [ ] Create `src/downloader/quota/middleware.py`
   - [ ] Check quota before processing request
   - [ ] Inject quota headers in response (X-RateLimit-Limit, X-RateLimit-Remaining, X-RateLimit-Reset)
   - [ ] Target: < 10ms overhead per request
   - Files: `src/downloader/quota/middleware.py`, `src/downloader/main.py`
   - Effort: 4h

1.4. **S4-BE-4: Create usage tracking endpoint**
   - [ ] `GET /usage` - Current usage statistics for API key
   - [ ] Return requests used, requests remaining, reset time
   - [ ] Include bandwidth usage if configured
   - Files: `src/downloader/routes/usage.py`
   - Effort: 3h

1.5. **S4-BE-5: Add 429 rate limit responses with Retry-After**
   - [ ] Return 429 Too Many Requests when quota exceeded
   - [ ] Include Retry-After header with seconds until reset
   - [ ] Add helpful error message with usage endpoint link
   - Files: `src/downloader/quota/middleware.py`
   - Effort: 2h

### 2. Testing (P1 - Required)

2.1. **S4-TEST-1: Add quota unit and integration tests**
   - [ ] Test quota model validation
   - [ ] Test counter increment and reset logic
   - [ ] Test middleware quota enforcement
   - [ ] Test usage endpoint responses
   - [ ] Test 429 response with Retry-After header
   - [ ] Test concurrent request handling
   - Files: `tests/unit/test_quota.py`, `tests/integration/test_quota.py`
   - Effort: 4h

### 3. Documentation (P1 - Required)

3.1. **S4-DOC-1: Document quota and usage endpoints**
   - [ ] Add quota configuration to deployment guide
   - [ ] Add usage endpoint to API reference
   - [ ] Document rate limit headers
   - [ ] Add quota troubleshooting section
   - Files: `docs/api/api-reference.md`, `docs/guides/deployment.md`
   - Effort: 2h

---

## Sprint 4 Summary

| Category | Tasks | Effort |
|----------|-------|--------|
| Backend (Core) | 5 | 15h |
| Testing | 1 | 4h |
| Documentation | 1 | 2h |
| **Total** | **7** | **21h** |

**Acceptance Criteria:**
- [ ] `GET /usage` returns current quota status
- [ ] 429 response includes Retry-After header
- [ ] Quota enforcement has < 10ms overhead per request
- [ ] Usage stats are accurate within 1% margin
- [ ] All quota tests pass

---

## Sprint 5 Preview - Ethical Crawling

**Priority:** P1 - Prevent IP bans, enable responsible crawling
**Focus:** robots.txt respect, configurable User-Agent

### Planned Tasks

| Task ID | Description | Effort |
|---------|-------------|--------|
| S5-BE-1 | Integrate robots.txt parser with Redis caching | 3h |
| S5-BE-2 | Add RESPECT_ROBOTS_TXT config flag (default: True) | 2h |
| S5-BE-3 | Add USER_AGENT config flag with default value | 1h |
| S5-BE-4 | Check robots.txt before every download request | 3h |
| S5-BE-5 | Return 403 with reason when URL disallowed | 2h |
| S5-TEST-1 | Add robots.txt unit and integration tests | 3h |
| S5-DOC-1 | Document ethical crawling configuration | 2h |

**Estimated Effort:** 16h

**Key Features:**
- robots.txt parsing using stdlib `urllib.robotparser` or `robotexclusionrulesparser`
- Redis caching of robots.txt with configurable TTL (default: 1 hour)
- Per-domain rate limiting based on Crawl-delay directive
- Configurable User-Agent for webmaster contact

---

## Sprint 6 Preview - Extended Features

**Priority:** P2 - Additional functionality
**Focus:** Screenshot/PDF output, webhook notifications

Candidates (to be prioritized):
- Screenshot/PDF output (`?output=screenshot|pdf`)
- Webhook notifications on job completion
- OCR (Image to Text)
- SDK/Client Libraries

---

## Completed Sprints

### Sprint 3 - Scheduling API ✅

**Completed:** 2026-01-21
**Archive:** [docs/features/scheduling.md](docs/features/scheduling.md)

7 tasks completed: APScheduler integration, schedule CRUD endpoints, job execution with retry, job history endpoint, 114 tests with 95% scheduler coverage.

### Sprint 2 - Browser Rendering ✅

**Completed:** 2026-01-19
**Archive:** [docs/features/browser-rendering.md](docs/features/browser-rendering.md)

10 tasks completed: Playwright integration, `?render=true` parameter, `?wait_for` selector support, browser process isolation, security hardening (file:// blocking, memory limits), 46 integration tests.

### Sprint 1 - Content Transformation ✅

**Completed:** 2026-01-18
**Archive:** [docs/features/content-transformation.md](docs/features/content-transformation.md)

7 tasks completed: HTML to Markdown conversion, plain text extraction, Accept header content negotiation, batch format support, 69 transformer tests.

### Sprint 0 - Production Readiness ✅

**Completed:** 2026-01-18
**Archive:** [docs/features/production-readiness.md](docs/features/production-readiness.md)

15 tasks completed across Testing/CI, Memory/Stability, Documentation, Infrastructure, and Code Quality.

---

## Notes

- OCR (Image to Text) and Screenshot/PDF output deferred to Sprint 6
- Phase 7 (Scheduling & Quotas) is current strategic priority
- See docs/roadmap.md for full feature roadmap
- See docs/features/ for completed work history

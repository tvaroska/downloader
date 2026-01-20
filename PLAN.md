# REST API Downloader - Sprint Plan

**Last Updated:** 2026-01-19

## Sprint 0 - Critical Remediation (MUST COMPLETE FIRST)

**Priority:** P0 - BLOCKING
**Status:** Not Started
**Reason:** Deliverable review identified critical gaps that must be addressed before Sprint 3 work continues.

See [FEEDBACK.md](FEEDBACK.md) for full assessment.

---

### 1. Test Coverage Gaps (P0 - Blocking)

1.1. **S0-TEST-1: Increase job_manager.py coverage from 40% to 75%+** ✅
   - [x] Add tests for Redis job storage operations
   - [x] Add tests for background job execution flow
   - [x] Add tests for job state transitions (pending → running → completed/failed)
   - [x] Add tests for job cleanup and expiration
   - [x] Add tests for error recovery scenarios
   - Files: `tests/test_job_manager.py`
   - **Completed:** 2026-01-20
   - **Result:** Coverage increased from 40% to **85.14%** (30 new tests added)
   - Plan: `.claude/plans/dazzling-popping-popcorn.md`

1.2. **S0-TEST-2: Increase routes/batch.py coverage from 41% to 75%+** ✅
   - [x] Add tests for job processing logic (lines 59-263)
   - [x] Add tests for result aggregation (lines 296-355)
   - [x] Add tests for concurrent URL processing
   - [x] Add tests for timeout handling and partial failures
   - Files: `tests/api/test_batch_processing.py`
   - **Completed:** 2026-01-20
   - **Result:** Coverage increased from 19% to **86.34%** (33 new tests added)
   - Plan: `.claude/plans/whimsical-zooming-floyd.md`

1.3. **S0-TEST-3: Increase metrics.py coverage from 45% to 70%+**
   - [ ] Add tests for metric recording paths (lines 149-293)
   - [ ] Add tests for aggregation logic
   - [ ] Add tests for time-series behavior
   - Files: `tests/test_metrics.py`

1.4. **S0-TEST-4: Increase routes/metrics.py coverage from 31% to 70%+**
   - [ ] Add tests for metrics endpoint responses (lines 19-81)
   - [ ] Add tests for metric retrieval edge cases
   - Files: `tests/api/test_metrics.py`

1.5. **S0-TEST-5: Increase browser/manager.py coverage from 64% to 75%+**
   - [ ] Add tests for pool initialization and shutdown
   - [ ] Add tests for browser acquisition timeout handling
   - [ ] Add tests for connection state management (lines 201-240)
   - Files: `tests/test_browser_manager.py`

1.6. **S0-TEST-6: Increase middleware.py coverage from 65% to 75%+**
   - [ ] Add tests for error path handling
   - [ ] Add tests for context cleanup on exceptions
   - Files: `tests/test_middleware.py`

---

### 2. Documentation Fixes (P0 - Blocking)

2.1. **S0-DOC-1: Fix broken documentation links**
   - [ ] Fix `docs/guides/monitoring.md` reference to `./MONITORING_IMPLEMENTATION.md`
   - [ ] Fix `examples/README.md` reference to non-existent `load_testing.py`
   - [ ] Verify all internal documentation links work
   - Files: `docs/guides/monitoring.md`, `examples/README.md`

2.2. **S0-DOC-2: Create VERSION file and fix version inconsistencies**
   - [ ] Create `VERSION` file with single source of truth
   - [ ] Update README.md health check example to use correct version
   - [ ] Update `src/downloader/__init__.py` to read from VERSION file
   - Files: `VERSION`, `README.md`, `src/downloader/__init__.py`

2.3. **S0-DOC-3: Create CHANGELOG.md**
   - [ ] Document changes from v0.0.1 to v0.3.0
   - [ ] Include migration notes for any breaking changes
   - [ ] Follow Keep a Changelog format
   - Files: `CHANGELOG.md`

2.4. **S0-DOC-4: Create SECURITY.md**
   - [ ] Add security policy and vulnerability reporting process
   - [ ] Document supported versions
   - [ ] Add security contact information
   - Files: `SECURITY.md`

2.5. **S0-DOC-5: Create CONTRIBUTING.md**
   - [ ] Add contribution guidelines
   - [ ] Document code style requirements
   - [ ] Add PR process and review expectations
   - Files: `CONTRIBUTING.md`

---

### 3. CI/CD Hardening (P1 - Required)

3.1. **S0-CI-1: Add type checking to CI**
   - [ ] Add mypy step to GitHub Actions workflow
   - [ ] Fix any type errors that mypy catches
   - [ ] Enforce strict type checking
   - Files: `.github/workflows/ci.yml`, `pyproject.toml`

3.2. **S0-CI-2: Add dependency vulnerability scanning**
   - [ ] Add `pip-audit` or `safety` to CI pipeline
   - [ ] Configure to fail on high/critical vulnerabilities
   - Files: `.github/workflows/ci.yml`

3.3. **S0-CI-3: Fix coverage threshold enforcement**
   - [ ] Ensure coverage reaches 70% (currently 68.2%)
   - [ ] After tests added, increase threshold to 75%
   - Files: `.github/workflows/ci.yml`, `.coveragerc`

---

### 4. Security Improvements (P1 - Required)

4.1. **S0-SEC-1: Fix CORS default configuration**
   - [ ] Change default CORS from `["*"]` to empty or localhost-only
   - [ ] Add clear documentation about production CORS configuration
   - [ ] Add production environment check that warns about wildcard CORS
   - Files: `src/downloader/config.py`, `docs/guides/configuration.md`

4.2. **S0-SEC-2: Add secrets detection to pre-commit**
   - [ ] Add `detect-secrets` or `gitleaks` to pre-commit config
   - [ ] Create baseline for existing files
   - Files: `.pre-commit-config.yaml`

---

### 5. Code Quality (P2 - Should Have)

5.1. **S0-QA-1: Clean up docs/archive/ folder**
   - [ ] Review archived bug files
   - [ ] Delete or clearly mark as resolved
   - [ ] Add README explaining archive purpose
   - Files: `docs/archive/`

5.2. **S0-QA-2: Add missing example file or remove reference**
   - [ ] Either create `examples/load_testing.py` or remove from documentation
   - [ ] Verify all example files run successfully
   - [ ] Add example testing to CI
   - Files: `examples/`

5.3. **S0-QA-3: Add document freshness tracking**
   - [ ] Add "Last Updated" header to all documentation files
   - [ ] Create documentation review schedule
   - Files: All docs/*.md files

---

## Sprint 0 Summary

| Category | Tasks | Priority |
|----------|-------|----------|
| Test Coverage | 6 | P0 |
| Documentation | 5 | P0 |
| CI/CD | 3 | P1 |
| Security | 2 | P1 |
| Code Quality | 3 | P2 |
| **Total** | **19** | - |

**Acceptance Criteria:**
- [ ] Overall test coverage ≥ 75%
- [ ] job_manager.py coverage ≥ 75%
- [ ] routes/batch.py coverage ≥ 75%
- [ ] All documentation links verified working
- [ ] CHANGELOG.md, SECURITY.md, CONTRIBUTING.md exist
- [ ] mypy passes in CI
- [ ] No critical/high dependency vulnerabilities
- [ ] Version consistency across all files

---

## Current Sprint: Sprint 3 (Scheduling API)

**Priority:** High - Enable recurring downloads for automated workflows
**Focus:** Cron-based scheduling CRUD API with APScheduler

---

## Sprint 3 - Scheduling API

### 1. Core Scheduling (P1 - Core)

1.1. **S3-BE-1: Add APScheduler dependency and scheduler service**
   - [ ] Add `apscheduler>=3.10.0` to pyproject.toml
   - [ ] Create `src/downloader/scheduler/service.py` with scheduler initialization
   - [ ] Configure Redis job store for persistence
   - [ ] Add scheduler startup/shutdown hooks to app lifecycle
   - Files: `pyproject.toml`, `src/downloader/scheduler/service.py`, `src/downloader/app.py`
   - Effort: 4h

1.2. **S3-BE-2: Implement schedule CRUD endpoints**
   - [ ] `POST /schedules` - Create scheduled job with cron expression
   - [ ] `GET /schedules` - List user's scheduled jobs
   - [ ] `GET /schedules/{id}` - Get schedule details and next run time
   - [ ] `DELETE /schedules/{id}` - Remove scheduled job
   - [ ] Add ScheduleCreate, ScheduleResponse Pydantic models
   - Files: `src/downloader/routes/schedules.py`, `src/downloader/models/schedule.py`
   - Effort: 6h

1.3. **S3-BE-3: Implement job execution logic**
   - [ ] Create job executor that calls download endpoint internally
   - [ ] Store job results in Redis with TTL (24h default)
   - [ ] Handle job failures with configurable retry (max 3 attempts)
   - [ ] Add job status tracking (pending, running, completed, failed)
   - Files: `src/downloader/scheduler/executor.py`, `src/downloader/scheduler/storage.py`
   - Effort: 4h

1.4. **S3-BE-4: Add job history endpoint**
   - [ ] `GET /schedules/{id}/history` - Get past executions
   - [ ] Include start time, duration, status, error message
   - [ ] Paginate results (default 20, max 100)
   - Files: `src/downloader/routes/schedules.py`
   - Effort: 2h

### 2. Testing (P1 - Required)

2.1. **S3-TEST-1: Add scheduler unit tests**
   - [ ] Test schedule CRUD operations
   - [ ] Test cron expression validation
   - [ ] Test job storage and retrieval
   - Files: `tests/unit/test_scheduler.py`
   - Effort: 3h

2.2. **S3-TEST-2: Add scheduler integration tests**
   - [ ] Test job execution with mock download
   - [ ] Test retry on failure
   - [ ] Test schedule persistence across restarts
   - Files: `tests/integration/test_scheduler.py`
   - Effort: 3h

### 3. Documentation (P1 - Required)

3.1. **S3-DOC-1: Document scheduling API**
   - [ ] Add scheduling endpoints to API reference
   - [ ] Add cron expression examples
   - [ ] Add scheduling guide with use cases
   - Files: `docs/api/api-reference.md`, `docs/guides/scheduling.md`
   - Effort: 2h

---

## Sprint 3 Summary

| Category | Tasks | Effort |
|----------|-------|--------|
| Backend (Core) | 4 | 16h |
| Testing | 2 | 6h |
| Documentation | 1 | 2h |
| **Total** | **7** | **24h** |

---

## Sprint 4 Preview - Quota Management

**Priority:** Medium - Usage limits for multi-tenant deployments
**Focus:** Per-API-key quotas, usage tracking, rate limiting

### Planned Tasks

| Task ID | Description | Effort |
|---------|-------------|--------|
| S4-BE-1 | Add quota configuration model | 2h |
| S4-BE-2 | Implement per-API-key request counting | 4h |
| S4-BE-3 | Add quota enforcement middleware | 4h |
| S4-BE-4 | Create usage tracking endpoint (`GET /usage`) | 3h |
| S4-BE-5 | Add 429 rate limit responses with Retry-After | 2h |
| S4-TEST-1 | Add quota unit and integration tests | 4h |
| S4-DOC-1 | Document quota and usage endpoints | 2h |

**Estimated Effort:** 21h

---

## Acceptance Criteria for Sprint 3 Completion

- [ ] `POST /schedules` creates a scheduled job with cron expression
- [ ] `GET /schedules` returns user's scheduled jobs
- [ ] Jobs execute within 1 minute of scheduled time
- [ ] Job history shows last 20 executions by default
- [ ] Failed jobs retry up to 3 times
- [ ] All scheduler tests pass
- [ ] API documentation updated with scheduling endpoints

---

## Completed Sprints

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

- OCR (Image to Text) and Screenshot/PDF output deferred to future sprints
- Phase 7 (Scheduling & Quotas) is current strategic priority
- See docs/roadmap.md for full feature roadmap
- See docs/features/ for completed work history

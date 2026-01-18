# REST API Downloader - Sprint Plan

## Current Sprint: Sprint 0 (Production Readiness)

**Priority:** Critical - Block production deployment until complete
**Estimated Effort:** 20-30 hours
**Focus:** Fix gaps identified in agency code review (FEEDBACK.md)

---

## Sprint 0 - Production Readiness

### 1. Testing & CI/CD (P0 - Critical)

1.1. ~~**S0-TEST-1: Fix version mismatch in tests**~~ ✅
   - Files: `tests/smoke/test_server_startup.py:14`, `tests/api/test_health.py:14,39`
   - Issue: Tests hardcode version `0.2.0` but actual version is `0.2.1`
   - Fix: Use dynamic version import or fixture
   - Effort: 15 minutes

1.2. ~~**S0-TEST-2: Fix test timeouts**~~ ✅
   - Issue: Full test suite hangs during coverage collection
   - Files: Various integration/e2e tests
   - Fix: Add proper timeouts, fix async cleanup, mock Playwright in unit tests
   - Effort: 2-3 hours
   - Plan: `.claude/plans/moonlit-coalescing-crane.md`

1.3. **S0-CICD-1: Add GitHub Actions CI pipeline**
   - Create: `.github/workflows/ci.yml`
   - Include: lint, type-check, smoke tests, build verification
   - Effort: 2 hours

1.4. **S0-CICD-2: Add test coverage reporting**
   - Add coverage threshold gate (target: 80%)
   - Upload to Codecov or similar
   - Effort: 1 hour

### 2. Memory & Stability (P0 - Critical)

2.1. ~~**S0-BUG-1: Fix unbounded caches in content_converter.py**~~ ✅
   - File: `src/downloader/content_converter.py:15-20`
   - Issue: 4 global caches (`_empty_content_cache`, `_fallback_bypass_cache`, `_js_heavy_cache`, `_static_html_cache`) grow indefinitely
   - Fix: Use `functools.lru_cache` or `cachetools.TTLCache` with max size
   - Effort: 1-2 hours

2.2. ~~**S0-BUG-2: Consolidate version to single source of truth**~~ ✅
   - Issue: Version in `__init__.py`, `config.py`, and tests all differ
   - Fix: Use `importlib.metadata.version()` in `__init__.py`, import `__version__` in `config.py`
   - Plan: `.claude/plans/generic-moseying-fairy.md`
   - Effort: 30 minutes

### 3. Documentation (P1 - High)

3.1. **S0-DOC-1: Update api-reference.md**
   - File: `doc/api-reference.md`
   - Issues:
     - Line 13: States "no authentication required" - false
     - Line 498-503: States "no rate limiting" - false
   - Fix: Update to reflect actual implemented features
   - Effort: 1 hour

3.2. **S0-DOC-2: Update PRD.md roadmap section**
   - File: `product/PRD.md:248-276`
   - Issue: Shows Phase 1-4 items as incomplete that are actually done
   - Fix: Mark completed items, update dates
   - Effort: 30 minutes

3.3. **S0-DOC-3: Create deployment runbook**
   - Create: `doc/DEPLOYMENT.md`
   - Include: Prerequisites, environment setup, Docker deployment, health verification
   - Effort: 2-3 hours

### 4. Docker & Infrastructure (P1 - High)

4.1. **S0-INFRA-1: Fix Dockerfile Python version**
   - File: `Dockerfile:2`
   - Issue: Uses Python 3.11, but dev uses 3.13 and pyproject.toml allows 3.10+
   - Fix: Match development version (3.13) or pin to tested version
   - Effort: 30 minutes

4.2. **S0-INFRA-2: Remove editable install from production**
   - File: `Dockerfile:29`
   - Issue: `pip install -e .` is for development
   - Fix: Use `pip install .` or build wheel first
   - Effort: 15 minutes

4.3. **S0-INFRA-3: Add docker-compose for local development**
   - Create: `docker-compose.dev.yml`
   - Include: App + Redis for full-stack local testing
   - Effort: 1 hour

### 5. Security (P1 - High)

5.1. **S0-SEC-1: Add security headers middleware**
   - File: `src/downloader/main.py`
   - Add: X-Content-Type-Options, X-Frame-Options, Referrer-Policy
   - Consider: Starlette's `SecureHeadersMiddleware` or custom
   - Effort: 1 hour

5.2. **S0-SEC-2: Document CORS production configuration**
   - Issue: Default CORS is `*` (wildcard)
   - Add: Warning in deployment docs, example restricted config
   - Effort: 30 minutes

### 6. Code Quality (P2 - Medium)

6.1. **S0-REFACTOR-1: Simplify HTTP client**
   - File: `src/downloader/http_client.py`
   - Issue: Over-engineered with priority queue and circuit breaker (per agency roadmap R3)
   - Fix: Remove priority queue, simplify to basic httpx client
   - Effort: 4-6 hours

6.2. **S0-REFACTOR-2: Extract Playwright context creation**
   - File: `src/downloader/content_converter.py`
   - Issue: Context creation duplicated in `render_html_with_playwright()` and `convert_content_with_playwright_fallback()`
   - Fix: Extract to shared helper function
   - Effort: 1 hour

6.3. **S0-REFACTOR-3: Replace magic numbers with config**
   - File: `src/downloader/content_converter.py:74,141`
   - Issue: Hardcoded `< 100` and `< 200` character thresholds
   - Fix: Add to ContentConfig
   - Effort: 30 minutes

---

## Sprint 0 Summary

| Category | Items | Estimated Hours |
|----------|-------|-----------------|
| Testing & CI/CD | 4 | 5-6 hours |
| Memory & Stability | 2 | 2-3 hours |
| Documentation | 3 | 4-5 hours |
| Docker & Infrastructure | 3 | 2 hours |
| Security | 2 | 1.5 hours |
| Code Quality | 3 | 6-8 hours |
| **Total** | **17** | **20-25 hours** |

---

## Acceptance Criteria for Sprint 0 Completion

- [ ] All smoke tests pass
- [ ] Full test suite completes without timeout
- [ ] CI pipeline runs on every PR
- [ ] Coverage report shows >80%
- [ ] No unbounded caches in codebase
- [ ] Documentation matches implemented features
- [ ] Dockerfile builds and runs correctly
- [ ] Deployment runbook tested and complete

---

## Future Sprints (Backlog)

### Sprint 1 - Performance & Reliability
- Content caching layer (Redis)
- Enhanced retry policies
- Webhook notifications
- OpenTelemetry integration

### Sprint 2 - Advanced Features
- Content preprocessing pipeline
- Multi-format transformation
- SDK/Client libraries

### Sprint 3 - Enterprise
- OAuth2/JWT authentication
- Usage analytics
- Multi-region deployment

---

## Notes

- This Sprint 0 represents remediation work that should have been delivered by the agency
- Priority order: P0 items block deployment, P1 items should be fixed within 1 week
- See FEEDBACK.md for detailed technical review findings

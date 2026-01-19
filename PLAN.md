# REST API Downloader - Sprint Plan

**Last Updated:** 2026-01-19

## Current Sprint: Sprint 2 (Browser Rendering)

**Priority:** High - Enable JavaScript-rendered content capture
**Focus:** Explicit render control and security hardening (Playwright already integrated)

---

## Sprint 2 - Browser Rendering

### 1. Playwright Integration (P1 - Core)

1.1. **S2-BE-1: Extract browser module from pdf_generator** ✅ DONE
   - [x] Add `playwright>=1.40.0` to pyproject.toml (already present)
   - [x] BrowserPool class exists in `pdf_generator.py` with queue-based management
   - [x] 30s timeout already implemented
   - [x] Extract BrowserPool to `src/downloader/browser/manager.py` for reuse
   - [x] Add 512MB memory limit per browser context via `--js-flags=--max-old-space-size`
   - Files: `src/downloader/browser/manager.py`, `src/downloader/pdf_generator.py`
   - Plan: `.claude/plans/streamed-leaping-pascal.md`

1.2. **S2-BE-2: Add explicit ?render=true parameter** ✅ DONE
   - [x] Auto-detection of JS-heavy pages exists (`needs_javascript_rendering()`)
   - [x] Playwright rendering works via `render_html_with_playwright()`
   - [x] Add `?render=true` query parameter to force rendering (bypass auto-detect)
   - [x] Wire parameter through to content processor
   - Files: `src/downloader/routes/download.py`, `src/downloader/services/content_processor.py`
   - Plan: `.claude/plans/groovy-snacking-pudding.md`

1.3. **S2-BE-3: Add wait_for selector support** ✅ DONE
   - [x] Basic `wait_for_load_state("networkidle")` exists
   - [x] Add `?wait_for=<selector>` query parameter
   - [x] Implement `page.wait_for_selector()` with configurable timeout
   - [x] Handle selector not found gracefully (timeout error → 408)
   - Files: `src/downloader/routes/download.py`, `src/downloader/content_converter.py`
   - Plan: `.claude/plans/peppy-kindling-piglet.md`

### 2. Process Isolation & Security (P1 - Required)

2.1. **S2-SEC-1: Implement browser process isolation** ✅ DONE
   - [x] Browsers run as separate Chromium processes (Playwright default)
   - [x] Health monitoring and usage tracking exists
   - [x] Add memory limits per browser context (512MB via --js-flags)
   - [x] Add explicit zombie process cleanup on timeout (SIGTERM/SIGKILL fallback)
   - Files: `src/downloader/browser/manager.py`
   - Plan: `.claude/plans/groovy-jingling-river.md`

2.2. **S2-SEC-2: Add browser security hardening** ✅ DONE
   - [x] `--disable-extensions`, `--disable-plugins` already set
   - [x] `--no-sandbox`, `--disable-dev-shm-usage` for container safety
   - [x] Remove `--disable-web-security` flag (currently enabled, insecure)
   - [x] Add `--disable-webgl` flag
   - [x] Add file:// URL blocking in validation layer
   - Files: `src/downloader/browser/manager.py`, `src/downloader/validation.py`
   - Plan: `.claude/plans/keen-booping-beaver.md`

### 3. Testing (P1 - Required)

3.1. **S2-TEST-1: Add browser rendering integration tests** ✅ DONE
   - [x] Create `tests/integration/test_browser_rendering.py`
   - [x] Test basic JS rendering (React/Vue hello world)
   - [x] Test wait_for selector functionality
   - [x] Test timeout handling
   - Files: `tests/integration/test_browser_rendering.py`
   - Plan: `.claude/plans/mighty-launching-hejlsberg.md`

3.2. **S2-TEST-2: Add browser security tests** ✅ DONE
   - [x] Test file:// URL blocking
   - [x] Test memory limit enforcement
   - [x] Test process cleanup after timeout
   - Files: `tests/integration/test_browser_security.py`
   - Plan: `.claude/plans/mighty-launching-hejlsberg.md`

### 4. Infrastructure (P1 - Required)

4.1. **S2-INFRA-1: Update Dockerfile for Playwright** ✅ DONE
   - [x] Playwright browser installation in Dockerfile
   - [x] System dependencies installed via `playwright install-deps`
   - [x] Container builds and runs with Chromium
   - Files: `Dockerfile`

4.2. **S2-DOC-1: Document browser rendering feature** ✅ DONE
   - [x] Auto-rendering behavior documented (implicit)
   - [x] Add `?render=true` parameter to API reference
   - [x] Add `?wait_for=<selector>` parameter to API reference
   - [x] Add examples for SPA scraping
   - Files: `docs/api/api-reference.md`

---

## Sprint 2 Summary

| Category | Remaining Work | Status |
|----------|----------------|--------|
| Backend (Core) | — | ✅ Done |
| Security | — | ✅ Done |
| Testing | — | ✅ Done |
| Infrastructure | — | ✅ Done |
| Documentation | — | ✅ Done |

---

## Acceptance Criteria for Sprint 2 Completion

- [x] `?render=true` returns JavaScript-rendered HTML (forces rendering)
- [x] `?wait_for=<selector>` waits for element before returning
- [x] Browser sessions timeout after 30 seconds (already implemented)
- [x] Memory usage limited to 512MB per session (via `--js-flags=--max-old-space-size`)
- [x] `--disable-web-security` removed, file:// URLs blocked
- [x] Dockerfile builds with Playwright support (already done)
- [x] All browser rendering tests pass
- [x] API documentation updated with new parameters

---

## Completed Sprints

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

- OCR (Image to Text) deferred to future sprint based on priority decision
- Phase 6 (Browser Rendering) is current strategic priority
- See docs/roadmap.md for full feature roadmap
- See docs/features/ for completed work history

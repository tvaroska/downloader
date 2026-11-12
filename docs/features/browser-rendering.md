# Browser Rendering - Feature History

**Last Updated:** 2026-11-12

## Overview

Capture JavaScript-rendered content, screenshots, and PDFs from SPAs and dynamic websites using Playwright-based browser automation with declarative allowlisted actions.

---

## Status: ✅ Complete (Sprint 2 + `v0.6.2` + `v0.7.0`)

- Sprint 2 completed: 2026-01-19 (JS rendering, `wait_for`, process isolation, security hardening)
- `v0.6.2` completed: 2026-11-12 (`S6-br-1` / `F-2026-11-05-002` single-pass `domcontentloaded` + bounded non-fatal `networkidle` settle)
- `v0.7.0` completed: 2026-11-12 (Screenshot/PDF output toggle + declarative allowlisted interaction scripting & `POST /render`)

---

## Completed Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Basic JS rendering (`?render=true`) | P1 | High | ✅ Complete |
| Wait for selectors (`?wait_for=`) | P1 | Medium | ✅ Complete |
| Process isolation & recycling (`BrowserPool`) | P1 | Medium | ✅ Complete |
| Security hardening (`context.route` SSRF guard, `file://` blocked) | P1 | Medium | ✅ Complete |
| Single-pass `domcontentloaded` + bounded `networkidle` settle (`v0.6.2`) | P1 | Low | ✅ Complete |
| Screenshot/PDF output toggle (`?output=screenshot\|pdf`, `Accept: image/png\|jpeg`) | P2 | Medium | ✅ Complete (`v0.7.0`) |
| Declarative interaction scripting (`?actions=`, `POST /render`, `/batch`) | P2 | High | ✅ Complete (`v0.7.0`) |

### API Implemented
- Query parameter: `?render=true` - Force JavaScript rendering
- Query parameter: `?wait_for=<selector>` - Wait for CSS selector before returning
- Query parameters: `?output=screenshot|pdf`, `?full_page=bool`, `?screenshot_format=png|jpeg`, and `Accept: image/png` / `image/jpeg`
- Query parameter & POST body: `?actions=<json>` and `POST /render` accepting up to `20` allowlisted actions (`click`, `fill`, `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; zero raw JS evaluation)
- Auto-detection: `needs_javascript_rendering()` for common SPA frameworks

### Success Criteria Met
- ✅ Successfully render React/Vue/Angular SPAs
- ✅ Browser sessions timeout after 30 seconds
- ✅ Memory usage limited to 512MB per session (via `--js-flags=--max-old-space-size`)
- ✅ `--disable-web-security` removed, file:// URLs blocked
- ✅ All 46 browser rendering/security tests passing

### Dependencies Added
```
playwright>=1.40.0
```
System: `playwright install chromium`

---

## Completed Work

### Sprint 2 - Browser Rendering (Completed 2026-01-19)

**Focus:** Explicit render control and security hardening

#### Playwright Integration (P1 - Core)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S2-BE-1 | Extract browser module from pdf_generator | `src/downloader/browser/manager.py`, `src/downloader/pdf_generator.py` | — |
| S2-BE-2 | Add explicit ?render=true parameter | `src/downloader/routes/download.py`, `src/downloader/services/content_processor.py` | — |
| S2-BE-3 | Add wait_for selector support | `src/downloader/routes/download.py`, `src/downloader/content_converter.py` | — |

#### Process Isolation & Security (P1 - Required)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S2-SEC-1 | Implement browser process isolation | `src/downloader/browser/manager.py` | — |
| S2-SEC-2 | Add browser security hardening | `src/downloader/browser/manager.py`, `src/downloader/validation.py` | — |

#### Testing (P1 - Required)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S2-TEST-1 | Add browser rendering integration tests | `tests/integration/test_browser_rendering.py` | — |
| S2-TEST-2 | Add browser security tests | `tests/integration/test_browser_security.py` | — |

#### Infrastructure (P1 - Required)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S2-INFRA-1 | Update Dockerfile for Playwright | `Dockerfile` | - |
| S2-DOC-1 | Document browser rendering feature | `spec/contracts.md` | - |

---

## Summary Statistics

| Category | Tasks Completed |
|----------|-----------------|
| Backend (Core) | 3 |
| Security | 2 |
| Testing | 2 |
| Infrastructure | 1 |
| Documentation | 1 |
| **Total** | **10** |

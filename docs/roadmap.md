# REST API Downloader - Strategic Roadmap

## Overview

This document outlines the strategic priorities and feature roadmap for the REST API Downloader service. Features are organized by area with clear prioritization.

---

## Current Status

| Phase | Status | Completion |
|-------|--------|------------|
| Phase 1: Core Functionality | Complete | 100% |
| Phase 2: Batch Processing | Complete | 100% |
| Phase 3: Performance & Reliability | Complete | 100% |
| Phase 4: Security & Production | In Progress | 80% |
| Phase 5: Content Transformation | Planned | 0% |
| Phase 6: Browser Rendering | Planned | 0% |
| Phase 7: Scheduling & Quotas | Planned | 0% |

---

## Priority Areas

### P0: Production Readiness (Phase 4 Completion)

**Goal**: Complete remaining production requirements before new feature work.

| Task | Priority | Status |
|------|----------|--------|
| Production deployment guide | P0 | Pending |

**Rationale**: Blocking for production deployment. Must complete before Phase 5.

---

### P1: Content Transformation (Phase 5)

**Goal**: Transform downloaded content into formats optimized for downstream consumption (LLMs, data pipelines).

#### Features

| Feature | Priority | Complexity | Dependencies |
|---------|----------|------------|--------------|
| HTML to Markdown | P1 | Medium | markdownify, BeautifulSoup |
| Image to Text (OCR) | P1 | High | pytesseract, Tesseract |
| Plain text extraction | P2 | Low | BeautifulSoup |

#### API Changes
- New query parameter: `?format=markdown|text|ocr`
- Batch support: `format` option in request body

#### Success Criteria
- Markdown output preserves document structure (headings, lists, links, code blocks)
- OCR accuracy > 90% for clean printed text
- Transformation adds < 500ms to response time

---

### P2: Browser Rendering (Phase 6)

**Goal**: Capture JavaScript-rendered content from SPAs and dynamic websites.

#### Features

| Feature | Priority | Complexity | Dependencies |
|---------|----------|------------|--------------|
| Basic JS rendering | P1 | High | Playwright |
| Wait for selectors | P1 | Medium | Playwright |
| Interaction scripting | P2 | High | Custom DSL |
| Screenshot/PDF output | P2 | Medium | Playwright |

#### API Changes
- New query parameter: `?render=true`
- Optional: `?wait_for=<selector>` or `?script=<encoded-script>`
- New output formats: `?output=screenshot|pdf`

#### Technical Considerations
- Resource limits: max 30s execution, 512MB memory per session
- Process isolation for security
- Browser pool management for performance
- Chromium binary management (playwright install)

#### Success Criteria
- Successfully render React/Vue/Angular SPAs
- Interaction scripts execute reliably
- Resource usage stays within defined limits

---

### P3: Scheduling & Quotas (Phase 7)

**Goal**: Enable recurring downloads and usage management for multi-tenant deployments.

#### Scheduling Features

| Feature | Priority | Complexity | Dependencies |
|---------|----------|------------|--------------|
| Cron-based scheduling | P1 | Medium | APScheduler, Redis |
| Schedule CRUD API | P1 | Low | FastAPI |
| Job history/logging | P2 | Low | Redis |
| Webhook on completion | P3 | Medium | httpx |

#### Quota Features

| Feature | Priority | Complexity | Dependencies |
|---------|----------|------------|--------------|
| Per-API-key request limits | P1 | Medium | Redis |
| Usage tracking endpoint | P1 | Low | FastAPI |
| Rate limit 429 responses | P1 | Low | Middleware |
| Tier-based plans | P3 | Medium | Config |

#### API Changes
- `POST /schedules` - Create scheduled job
- `GET /schedules` - List user's scheduled jobs
- `GET /schedules/{id}` - Get schedule details
- `DELETE /schedules/{id}` - Remove scheduled job
- `GET /usage` - Current usage statistics

#### Success Criteria
- Cron expressions execute within 1 minute of scheduled time
- Quota enforcement has < 10ms overhead per request
- Usage stats are accurate within 1% margin

---

## Future Considerations (Backlog)

These features are not prioritized for immediate development but may be considered later:

| Feature | Description | Complexity |
|---------|-------------|------------|
| Webhook notifications | Push results to configured endpoints | Medium |
| Content diffing | Track changes between downloads | Medium |
| Advanced caching | Content-aware TTLs, pre-warming | Medium |
| Multi-region support | Distributed deployment | High |
| GraphQL API | Alternative query interface | Medium |
| Bandwidth quotas | Limit bytes downloaded per key | Medium |

---

## Technical Dependencies

### Phase 5 Dependencies
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0
pytesseract>=0.3.10
Pillow>=10.0.0
```
System: `tesseract-ocr` package

### Phase 6 Dependencies
```
playwright>=1.40.0
```
System: `playwright install chromium`

### Phase 7 Dependencies
```
apscheduler>=3.10.0
```
Or alternatively: `celery[redis]>=5.3.0`

---

## Risk Matrix

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Browser rendering OOM | High | Medium | Process isolation, memory limits |
| OCR accuracy issues | Medium | Medium | Cloud API fallback option |
| Scheduler job failures | Medium | Low | Dead-letter queue, retry policies |
| Quota bypass attempts | Medium | Low | Server-side enforcement only |

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-18 | Prioritize content transformation over browser rendering | Lower complexity, immediate value for LLM use cases |
| 2026-01-18 | Use Playwright over Puppeteer | Better Python support, maintained by Microsoft |
| 2026-01-18 | Per-API-key quotas first | Simpler than tiered plans, covers most use cases |
| 2026-01-18 | Cron expressions over simple intervals | Industry standard, more flexible |

---

## Next Steps

1. **Immediate**: Complete S0-DOC-3 (Production deployment guide)
2. **Phase 5 Start**: HTML to Markdown transformation
3. **Phase 5**: Add OCR capability
4. **Phase 6**: Playwright integration for browser rendering
5. **Phase 7**: Scheduling and quota management

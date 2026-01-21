# REST API Downloader - Strategic Roadmap

**Last Updated:** 2026-01-19

## Overview

This document outlines the strategic priorities and feature roadmap for the REST API Downloader service. Features are organized by area with clear prioritization.

---

## Current Status

| Phase | Status | Completion |
|-------|--------|------------|
| Phase 1: Core Functionality | ✅ Complete | 100% |
| Phase 2: Batch Processing | ✅ Complete | 100% |
| Phase 3: Performance & Reliability | ✅ Complete | 100% |
| Phase 4: Security & Production | ✅ Complete | 100% |
| Phase 5: Content Transformation | 🚧 Partial | 70% |
| Phase 6: Browser Rendering | 🚧 Partial | 60% |
| Phase 7: Scheduling & Quotas | 🚧 In Progress | 0% |

---

## Current Quarter Focus

### Active: Phase 7 - Scheduling & Quotas

**Sprint 3** (Current): Scheduling CRUD API
- 7 tasks, ~24 hours effort
- See [PLAN.md](../PLAN.md) for detailed task breakdown

**Sprint 4** (Next): Quota Management
- 7 tasks, ~21 hours effort

### Completed: Phase 6 - Browser Rendering (Partial)

**Sprint 2** (Complete): Playwright-based JS Rendering
- 10 tasks completed
- Core rendering (`?render=true`, `?wait_for`) complete
- Screenshot/PDF output deferred to future sprint
- See [docs/features/browser-rendering.md](features/browser-rendering.md)

---

## Priority Areas

### ✅ P0: Production Readiness (Phase 4 - Complete)

**Completed:** 2026-01-18
**Archive:** [docs/features/production-readiness.md](features/production-readiness.md)

15 tasks completed: CI/CD pipeline, test coverage, documentation updates, Docker fixes, code quality improvements.

---

### 🚧 P1: Content Transformation (Phase 5 - Partial)

**Goal**: Transform downloaded content into formats optimized for downstream consumption (LLMs, data pipelines).

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| HTML to Markdown | P1 | Medium | ✅ Complete |
| Plain text extraction | P2 | Low | ✅ Complete |
| Content negotiation | P1 | Medium | ✅ Complete |
| Image to Text (OCR) | P1 | High | 📋 Backlog |

#### API Implemented
- Accept header negotiation: `text/markdown`, `text/plain`, `text/html`
- Batch support: `format` and `default_format` options in request body

#### Success Criteria Met
- ✅ Markdown output preserves document structure (headings, lists, links, code blocks)
- ✅ Transformation adds < 500ms to response time (measured: 53-110ms overhead)

#### OCR Deferred
OCR (pytesseract) deferred to future sprint based on priority decision.

---

### 🚧 P2: Browser Rendering (Phase 6 - Partial)

**Goal**: Capture JavaScript-rendered content from SPAs and dynamic websites.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Basic JS rendering | P1 | High | ✅ Complete |
| Wait for selectors | P1 | Medium | ✅ Complete |
| Process isolation | P1 | Medium | ✅ Complete |
| Security hardening | P1 | Medium | ✅ Complete |
| Screenshot/PDF output | P2 | Medium | 📋 Backlog |
| Interaction scripting | P2 | High | 📋 Backlog |

#### API Implemented
- Query parameter: `?render=true` - Force JavaScript rendering
- Query parameter: `?wait_for=<selector>` - Wait for CSS selector

#### Technical Implementation
- Resource limits: max 30s execution, 512MB memory per session
- Process isolation with explicit zombie cleanup
- Browser pool management with health monitoring
- Security: file:// blocking, `--disable-web-security` removed

#### Success Criteria Met
- ✅ Successfully render React/Vue/Angular SPAs
- ✅ Resource usage stays within defined limits
- ✅ 46 integration tests passing

#### Future Work
- Screenshot/PDF output (`?output=screenshot|pdf`)
- Interaction scripting (custom DSL for click/type/scroll)

---

### 🚧 P3: Scheduling & Quotas (Phase 7 - In Progress)

**Goal**: Enable recurring downloads and usage management for multi-tenant deployments.

#### Scheduling Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Cron-based scheduling | P1 | Medium | 🚧 Sprint 3 |
| Schedule CRUD API | P1 | Low | 🚧 Sprint 3 |
| Job history/logging | P2 | Low | 🚧 Sprint 3 |
| Webhook on completion | P3 | Medium | 📋 Backlog |

#### Quota Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Per-API-key request limits | P1 | Medium | 📋 Sprint 4 |
| Usage tracking endpoint | P1 | Low | 📋 Sprint 4 |
| Rate limit 429 responses | P1 | Low | 📋 Sprint 4 |
| Tier-based plans | P3 | Medium | 📋 Backlog |

#### API Changes (Planned)
- `POST /schedules` - Create scheduled job
- `GET /schedules` - List user's scheduled jobs
- `GET /schedules/{id}` - Get schedule details
- `DELETE /schedules/{id}` - Remove scheduled job
- `GET /schedules/{id}/history` - Get past executions
- `GET /usage` - Current usage statistics

#### Success Criteria
- Cron expressions execute within 1 minute of scheduled time
- Quota enforcement has < 10ms overhead per request
- Usage stats are accurate within 1% margin

#### Current Sprint
- **Sprint 3**: Scheduling CRUD API, cron execution, job history
- **Sprint 4**: Quota management, usage tracking, rate limiting

---

## Future Considerations (Backlog)

These features are not prioritized for immediate development but may be considered later:

### Operations & DevOps

| Feature | Description | Complexity | Personas |
|---------|-------------|------------|----------|
| Prometheus /metrics endpoint | Native Prometheus metrics for monitoring | Low | Sarah (DevOps) |
| Graceful shutdown with drain | Zero-downtime deployments, connection draining | Low | Sarah (DevOps) |
| OpenTelemetry integration | Distributed tracing, OTLP export | Medium | Sarah (DevOps) |

### Developer Experience

| Feature | Description | Complexity | Personas |
|---------|-------------|------------|----------|
| SDK/Client Libraries | Python, JavaScript, Go clients with code generation | Medium | Maya (API Dev), Alex (Analyst) |
| GraphQL API | Alternative query interface | Medium | Maya (API Dev) |

### Content & Processing

| Feature | Description | Complexity | Personas |
|---------|-------------|------------|----------|
| Webhook notifications | Push results to configured endpoints | Medium | Maya, David (Pipeline) |
| Content diffing | Track changes between downloads | Medium | Maya, Alex |
| Advanced caching | Content-aware TTLs, pre-warming | Medium | David (Pipeline) |
| OCR (Image to Text) | Pytesseract-based text extraction | High | Alex (Analyst) |
| Screenshot/PDF output | Browser-based capture | Medium | Alex (Analyst) |
| Interaction scripting | Custom DSL for browser automation | High | Alex (Analyst) |

### Infrastructure

| Feature | Description | Complexity | Personas |
|---------|-------------|------------|----------|
| Multi-region support | Distributed deployment | High | Sarah (DevOps) |
| Bandwidth quotas | Limit bytes downloaded per key | Medium | Sarah (DevOps) |

---

## Technical Dependencies

### Phase 5 Dependencies (Partial)
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0
```

### Phase 5 Dependencies (Deferred - OCR)
```
pytesseract>=0.3.10
Pillow>=10.0.0
```
System: `tesseract-ocr` package

### Phase 6 Dependencies (Complete)
```
playwright>=1.40.0
```
System: `playwright install chromium`

### Phase 7 Dependencies (Current)
```
apscheduler>=3.10.0
```
Or alternatively: `celery[redis]>=5.3.0`

---

## Risk Matrix

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Browser rendering OOM | High | Medium | Process isolation, memory limits ✅ |
| OCR accuracy issues | Medium | Medium | Cloud API fallback option |
| Scheduler job failures | Medium | Low | Dead-letter queue, retry policies |
| Quota bypass attempts | Medium | Low | Server-side enforcement only |

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-19 | Add graceful shutdown, Prometheus metrics, SDK to backlog | DevOps (Sarah) and developer experience priorities from persona analysis |
| 2026-01-19 | Sprint 2 complete, move to Phase 7 | Browser rendering core complete, scheduling higher priority |
| 2026-01-19 | Defer Screenshot/PDF output | Core rendering sufficient, scheduling more valuable |
| 2026-01-18 | Sprint 1 complete, move to Phase 6 | Content transformation (Markdown/text) complete |
| 2026-01-18 | Defer OCR, prioritize browser rendering | Browser rendering provides more immediate value |
| 2026-01-18 | Sprint 0 complete, move to Phase 5 | Production readiness achieved |
| 2026-01-18 | Prioritize content transformation over browser rendering | Lower complexity, immediate value for LLM use cases |
| 2026-01-18 | Use Playwright over Puppeteer | Better Python support, maintained by Microsoft |
| 2026-01-18 | Per-API-key quotas first | Simpler than tiered plans, covers most use cases |
| 2026-01-18 | Cron expressions over simple intervals | Industry standard, more flexible |

---

## Release Plan

### Release 0.3.0 - Content Transformation (Complete)
- ✅ HTML to Markdown conversion
- ✅ Plain text extraction
- ✅ Accept header content negotiation
- OCR deferred to future release

### Release 0.4.0 - Browser Rendering (Complete)
- ✅ Playwright-based JS rendering
- ✅ Wait for selectors
- ✅ Process isolation and security
- Screenshot/PDF output deferred

### Release 0.5.0 - Scheduling (Current)
- Cron-based scheduling API
- Schedule CRUD endpoints
- Job history and logging
- Target: Sprint 3 completion

### Release 0.6.0 - Quotas
- Per-API-key quotas
- Usage tracking endpoint
- Rate limit 429 responses
- Target: Sprint 4 completion

### Release 0.7.0 - Extended Features (Future)
- Screenshot/PDF output
- Webhook notifications
- OCR (Image to Text)
- Target: TBD based on priorities

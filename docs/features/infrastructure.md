# Infrastructure - Feature History

**Last Updated:** 2026-01-21

## Overview

This document tracks infrastructure and operational improvements for the REST API Downloader service, including architecture changes, observability, and deployment concerns.

---

## Status: 📋 Planned

---

## Planned Work

### Split Worker Architecture (Priority: P2 - Medium, Effort: High)
- [ ] Design worker architecture with Redis as message broker
- [ ] Extract PlaywrightPDFGenerator to separate worker container
- [ ] Extract SchedulerService to worker container
- [ ] Implement job queue with Redis
- [ ] Update docker-compose for multi-container setup
- [ ] Add worker health checks and monitoring
- **Problem:** Heavy PDF generation and Playwright processes consume CPU/memory, causing API timeouts and degraded performance for all users. Cannot scale API and workers independently based on demand.
- **Priority:** P2 - Medium
- **Estimated Effort:** High
- **Status:** 📋 Planned
- **Added:** 2026-01-21

### OpenTelemetry Observability (Priority: P2 - Medium, Effort: Medium)
- [ ] Add OpenTelemetry SDK dependencies
- [ ] Instrument API endpoints with tracing
- [ ] Add Redis operation spans
- [ ] Add external HTTP request spans
- [ ] Configure OTLP exporter
- [ ] Add correlation IDs across request chain
- **Problem:** Network services are prone to external failures (DNS, timeouts, 5xx). Logs are not enough to diagnose issues across the API -> Redis -> External Site path.
- **Priority:** P2 - Medium
- **Estimated Effort:** Medium
- **Status:** 📋 Planned
- **Added:** 2026-01-21

---

## Completed Work

None yet.

---

## Related Documentation

- [Production Readiness](production-readiness.md) - Sprint 0 infrastructure work
- [Roadmap](../roadmap.md) - Strategic priorities

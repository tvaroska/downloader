# Downloader Core - Feature History

**Last Updated:** 2026-01-21

## Overview

This document tracks all core downloading functionality features for the REST API Downloader service, including HTTP handling, crawling behavior, and client configuration.

---

## Status: 📋 Sprint 5 Planned

---

## Completed Work

*No core downloader features completed yet.*

---

## In Progress

*None currently.*

---

## Planned Work

### Ethical Crawling (Sprint 5 - Next)

**Target Sprint:** Sprint 5
**Priority:** P1 - High
**Estimated Effort:** 16h

#### Tasks

| Task ID | Description | Effort |
|---------|-------------|--------|
| S5-BE-1 | Integrate robots.txt parser with Redis caching | 3h |
| S5-BE-2 | Add RESPECT_ROBOTS_TXT config flag (default: True) | 2h |
| S5-BE-3 | Add USER_AGENT config flag with default value | 1h |
| S5-BE-4 | Check robots.txt before every download request | 3h |
| S5-BE-5 | Return 403 with reason when URL disallowed | 2h |
| S5-TEST-1 | Add robots.txt unit and integration tests | 3h |
| S5-DOC-1 | Document ethical crawling configuration | 2h |

#### Key Features
- robots.txt parsing using stdlib `urllib.robotparser` or `robotexclusionrulesparser`
- Redis caching of robots.txt with configurable TTL (default: 1 hour)
- Per-domain rate limiting based on Crawl-delay directive
- Configurable User-Agent for webmaster contact

#### Problem Statement
Users get IP bans from crawling without respecting robots.txt, and need to crawl responsibly with proper identification for webmaster contact.

- **Added:** 2026-01-21

---

## Summary Statistics

| Category | Tasks Completed |
|----------|-----------------|
| Ethical Crawling | 0 |
| **Total** | **0** |

---

## Technical Dependencies

### Ethical Crawling
```
robotexclusionrulesparser>=1.7.1  # or use stdlib urllib.robotparser
redis>=4.0.0  # for caching
```

---

## Decision Log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-01-21 | Add Ethical Crawling feature | Prevent IP bans and ensure responsible crawling behavior |
| 2026-01-21 | Default RESPECT_ROBOTS_TXT to True | Ethical default, users can opt-out if needed |
| 2026-01-21 | Use Redis for robots.txt caching | Consistent with existing caching infrastructure |

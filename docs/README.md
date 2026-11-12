# Documentation

**Last Updated:** 2026-11-12

Technical documentation for the REST API Downloader.

## Documentation Index

### API Documentation
- **[API Contracts](../spec/contracts.md)** - Complete API documentation with endpoints, schemas, and examples (the WHAT — lives in `spec/`)

### Implementation Guides
- **[Deployment Guide](./guides/deployment.md)** - Production deployment runbook with Docker, configuration, and security
- **[Configuration Management](./guides/configuration.md)** - Comprehensive guide to Pydantic Settings-based configuration system
- **[Monitoring Implementation](./guides/monitoring.md)** - Production monitoring setup with Prometheus and Grafana
- **[Scheduling Guide](./guides/scheduling.md)** - Cron-based scheduled download setup and usage

### Strategic Planning
- **[Product Roadmap](./roadmap.md)** - Canonical strategic priorities, release milestones, and deferred backlog
- **[Feature History](./features/)** - Completed sprint and feature archives

### Quick Links
- **[README](../README.md)** - Project overview and getting started guide
- **[Examples](../examples/README.md)** - Practical usage examples
- **[Requirements (spec/)](../spec/)** - API contracts and product requirements (the WHAT)
- **[Roadmap](./roadmap.md)** - Strategic backlog · **[Decisions](../DECISIONS.md)** · **[Critical paths](../CRITICAL.md)**

## Documentation Structure

```
docs/
├── README.md                    # This file - documentation index
├── roadmap.md                   # Strategic priorities
├── features/                    # Feature history (planning system)
├── guides/
│   ├── configuration.md         # Configuration system
│   ├── deployment.md            # Production deployment
│   └── monitoring.md            # Observability setup
└── archive/                     # Historical documentation
    ├── BUG_FIX_SUMMARY.md       # Resolved bug fixes
    ├── DOWNLOADER_BUG.md        # Bug reports
    └── TEST_RESULTS.md          # Test findings

spec/                            # THE WHAT — requirements (status-free)
└── contracts.md                 # API endpoints and schemas

examples/
└── README.md                    # Usage examples and guides

monitoring/
└── README.md                    # Monitoring stack setup
```

## Quick Start

1. **Installation**: See [README](../README.md)
2. **Configuration**: Copy `.env.example` and customize (see [Configuration](./guides/configuration.md))
3. **API Usage**: Review [API Contracts](../spec/contracts.md) and [Examples](../examples/README.md)
4. **Monitoring**: Set up observability using [Monitoring Guide](./guides/monitoring.md)

## Finding Information

**For API Usage:**
- Start with [API Contracts](../spec/contracts.md)
- See [Examples](../examples/README.md) for practical code

**For Configuration:**
- Read [Configuration Management](./guides/configuration.md)
- Check `.env.example` for all available settings

**For Operations:**
- See [Monitoring Implementation](./guides/monitoring.md)
- Review [Product Roadmap](./roadmap.md) for current status

**For Strategic Planning:**
- Review [Product Roadmap](./roadmap.md) - strategic priorities

## Recent Updates

### November 2026 (`v0.6.2` & `v0.7.0`)
- **`v0.6.2` (`S6-br-1` / `F-2026-11-05-002`):** Replaced two-pass `networkidle` browser navigation with single-pass `domcontentloaded` + bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`) in `src/downloader/content_converter.py`
- **`v0.7.0` Extended Features:** Added screenshot/PDF query toggles (`?output=screenshot|pdf`, `Accept: image/png|jpeg`), declarative allowlisted Playwright actions (`POST /render`, `?actions=`, `/batch`), SSRF-safe HMAC-SHA256 webhooks on batch/schedule completion, Tesseract OCR (`pytesseract` + `Pillow`), and typed client SDKs in [`sdks/python`](../sdks/python/README.md), [`sdks/javascript`](../sdks/javascript/README.md), and [`sdks/go`](../sdks/go)
- **`v0.7.0` Operations & Infrastructure:** Added graceful shutdown connection drain (`src/downloader/drain.py`), split API+worker architecture (`DEPLOYMENT_MODE=all|api|worker`, `docker-compose.split.yml`), and opt-in OpenTelemetry distributed tracing (`OTEL_ENABLED`) — archived in [docs/features/infrastructure.md](./features/infrastructure.md)

### November 2026 (`v0.6.0` & `v0.6.1`)
- Delivered Sprint 4 Ethical Crawling (`robots.txt` compliance, `Crawl-delay` throttling, `HTTP 403` `robots_disallowed`, and Prometheus `robots_*` metrics) — archived in [docs/features/ethical-crawling.md](./features/ethical-crawling.md)
- Documented `v0.6.0` production hardening (`HTTP 413` streaming `CONTENT_MAX_DOWNLOAD_SIZE`, redirect & Playwright SSRF route blocking, active `HTTP 429` rate limiting, browser pool recycling & idle scale-to-zero, JSON `uvicorn.access` fields, and unprivileged container port `:8000`)
- Consolidated project tracking to pure Roadmap-first ([docs/roadmap.md](./roadmap.md) + [DECISIONS.md](../DECISIONS.md))

### January 2026
- Reorganized documentation structure (merged doc/ into docs/)
- Archived resolved bug documentation to docs/archive/
- Added Phases 5-7 to roadmap (Content Transformation, Browser Rendering, Scheduling)

### October 2025
- Documentation Consolidation: Merged planning docs into docs/roadmap.md
- SSRF Protection: Implemented comprehensive SSRF protection
- Configuration System: Added Pydantic Settings-based configuration
- Structured Logging: Separate access/error handlers with JSON support

## Contributing

When adding new documentation:
- Technical implementation guides -> `docs/guides/`
- API contracts / requirements -> `spec/contracts.md`
- Strategic planning -> `docs/roadmap.md`
- Usage examples -> `examples/`
- Operational guides -> Component-specific directories (e.g., `monitoring/`)

Keep documentation:
- **Accurate**: Update when code changes
- **Concise**: Focus on essential information
- **Practical**: Include code examples
- **Searchable**: Use clear headings and structure

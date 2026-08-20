# Documentation

**Last Updated:** 2026-01-20

Technical documentation for the REST API Downloader.

## Documentation Index

### API Documentation
- **[API Contracts](../spec/contracts.md)** - Complete API documentation with endpoints, schemas, and examples (the WHAT — lives in `spec/`)

### Implementation Guides
- **[Deployment Guide](./guides/deployment.md)** - Production deployment runbook with Docker, configuration, and security
- **[Configuration Management](./guides/configuration.md)** - Comprehensive guide to Pydantic Settings-based configuration system
- **[Monitoring Implementation](./guides/monitoring.md)** - Production monitoring setup with Prometheus and Grafana

### Strategic Planning
- **[Product Roadmap](./roadmap.md)** - Strategic priorities and feature roadmap
- **[Feature History](./features/)** - Completed feature documentation

### Quick Links
- **[README](../README.md)** - Project overview and getting started guide
- **[Examples](../examples/README.md)** - Practical usage examples
- **[Requirements (spec/)](../spec/)** - API contracts and product requirements (the WHAT)
- **[TODO](../TODO.md)** - Live sprint status · **[Decisions](../DECISIONS.md)** · **[Critical paths](../CRITICAL.md)**

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

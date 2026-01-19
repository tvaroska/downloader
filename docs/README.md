# Documentation

Technical documentation for the REST API Downloader.

## Documentation Index

### API Documentation
- **[API Reference](./api/api-reference.md)** - Complete API documentation with endpoints, schemas, and examples

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
- **[Product Documents](../product/)** - PRD, architecture, metrics

## Documentation Structure

```
docs/
├── README.md                    # This file - documentation index
├── roadmap.md                   # Strategic priorities
├── features/                    # Feature history (planning system)
├── api/
│   └── api-reference.md         # API endpoints and schemas
├── guides/
│   ├── configuration.md         # Configuration system
│   ├── deployment.md            # Production deployment
│   └── monitoring.md            # Observability setup
└── archive/                     # Historical documentation
    ├── BUG_FIX_SUMMARY.md       # Resolved bug fixes
    ├── DOWNLOADER_BUG.md        # Bug reports
    └── TEST_RESULTS.md          # Test findings

product/
├── PRD.md                       # Product requirements
├── architecture.md              # Technical architecture
├── metrics.md                   # Success metrics
├── personas.md                  # User personas
└── user-journeys.md             # User journey maps

examples/
└── README.md                    # Usage examples and guides

monitoring/
└── README.md                    # Monitoring stack setup
```

## Quick Start

1. **Installation**: See [README](../README.md)
2. **Configuration**: Copy `.env.example` and customize (see [Configuration](./guides/configuration.md))
3. **API Usage**: Review [API Reference](./api/api-reference.md) and [Examples](../examples/README.md)
4. **Monitoring**: Set up observability using [Monitoring Guide](./guides/monitoring.md)

## Finding Information

**For API Usage:**
- Start with [API Reference](./api/api-reference.md)
- See [Examples](../examples/README.md) for practical code

**For Configuration:**
- Read [Configuration Management](./guides/configuration.md)
- Check `.env.example` for all available settings

**For Operations:**
- See [Monitoring Implementation](./guides/monitoring.md)
- Review [Product Roadmap](./roadmap.md) for current status

**For Strategic Planning:**
- Review [Product Roadmap](./roadmap.md) - strategic priorities
- Check [PRD](../product/PRD.md) for product requirements

## Recent Updates

### January 2026
- Reorganized documentation structure (merged doc/ into docs/)
- Archived resolved bug documentation to docs/archive/
- Added Phases 5-7 to roadmap (Content Transformation, Browser Rendering, Scheduling)

### October 2025
- Documentation Consolidation: Merged planning docs into product/roadmap.md
- SSRF Protection: Implemented comprehensive SSRF protection
- Configuration System: Added Pydantic Settings-based configuration
- Structured Logging: Separate access/error handlers with JSON support

## Contributing

When adding new documentation:
- Technical implementation guides -> `docs/guides/`
- API documentation -> `docs/api/`
- Strategic planning -> `product/`
- Usage examples -> `examples/`
- Operational guides -> Component-specific directories (e.g., `monitoring/`)

Keep documentation:
- **Accurate**: Update when code changes
- **Concise**: Focus on essential information
- **Practical**: Include code examples
- **Searchable**: Use clear headings and structure

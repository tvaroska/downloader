# Documentation

Technical documentation for the REST API Downloader.

## 📚 Documentation Index

### API Documentation
- **[API Reference](./api-reference.md)** - Complete API documentation with endpoints, schemas, and examples

### Implementation Guides
- **[Deployment Guide](./DEPLOYMENT.md)** - Production deployment runbook with Docker, configuration, and security
- **[Configuration Management](./CONFIGURATION_MANAGEMENT.md)** - Comprehensive guide to Pydantic Settings-based configuration system
- **[Monitoring Implementation](./MONITORING_IMPLEMENTATION.md)** - Production monitoring setup with Prometheus and Grafana

### Quick Links
- **[Product Roadmap](../product/roadmap.md)** - Strategic roadmap with completed refactoring and enhancement priorities
- **[README](../README.md)** - Project overview and getting started guide
- **[Examples](../examples/README.md)** - Practical usage examples

## 🔍 Documentation Structure

```
doc/
├── README.md                        # This file - documentation index
├── api-reference.md                 # API endpoints and schemas
├── DEPLOYMENT.md                    # Production deployment guide
├── CONFIGURATION_MANAGEMENT.md      # Configuration system guide
└── MONITORING_IMPLEMENTATION.md     # Monitoring setup guide

product/
├── roadmap.md                       # Strategic roadmap (consolidated)
├── PRD.md                           # Product requirements
├── architecture.md                  # Technical architecture
├── metrics.md                       # Success metrics
├── personas.md                      # User personas
└── user-journeys.md                 # User journey maps

examples/
└── README.md                        # Usage examples and guides

monitoring/
└── README.md                        # Monitoring stack setup
```

## 📖 Documentation Guidelines

### Finding Information

**For API Usage:**
- Start with [API Reference](./api-reference.md)
- See [Examples](../examples/README.md) for practical code

**For Configuration:**
- Read [Configuration Management](./CONFIGURATION_MANAGEMENT.md)
- Check `.env.example` for all available settings

**For Operations:**
- See [Monitoring Implementation](./MONITORING_IMPLEMENTATION.md)
- Review [Product Roadmap](../product/roadmap.md) for current status

**For Strategic Planning:**
- Review [Product Roadmap](../product/roadmap.md) - single source of truth
- Check [PRD](../product/PRD.md) for product requirements

## 🚀 Quick Start

1. **Installation**: See [README](../README.md)
2. **Configuration**: Copy `.env.example` and customize (see [Configuration Management](./CONFIGURATION_MANAGEMENT.md))
3. **API Usage**: Review [API Reference](./api-reference.md) and [Examples](../examples/README.md)
4. **Monitoring**: Set up observability using [Monitoring Implementation](./MONITORING_IMPLEMENTATION.md)

## 📝 Recent Updates

### October 2025
- ✅ **Documentation Consolidation**: Merged `recommendations.md` and `PROGRESS.md` into `product/roadmap.md`
- ✅ **SSRF Protection**: Implemented comprehensive SSRF protection (see roadmap)
- ✅ **Configuration System**: Added Pydantic Settings-based configuration (see Configuration Management guide)
- ✅ **Structured Logging**: Separate access/error handlers with JSON support (see Configuration Management guide)

## 🤝 Contributing

When adding new documentation:
- Technical implementation guides → `doc/`
- Strategic planning → `product/`
- Usage examples → `examples/`
- Operational guides → Component-specific directories (e.g., `monitoring/`)

Keep documentation:
- **Accurate**: Update when code changes
- **Concise**: Focus on essential information
- **Practical**: Include code examples
- **Searchable**: Use clear headings and structure

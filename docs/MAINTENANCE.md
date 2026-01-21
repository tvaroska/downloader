# Documentation Maintenance

**Last Updated:** 2026-01-20

Guidelines for keeping documentation accurate and up-to-date.

## Freshness Tracking

All documentation files use a `**Last Updated:** YYYY-MM-DD` header after the main title. Update this date when making significant content changes.

## Review Schedule

| Document | Review Frequency | Owner |
|----------|-----------------|-------|
| `docs/api/api-reference.md` | Every release | Backend team |
| `docs/guides/deployment.md` | Monthly | DevOps |
| `docs/guides/configuration.md` | Every config change | Backend team |
| `docs/guides/monitoring.md` | Quarterly | DevOps |
| `docs/roadmap.md` | Weekly | Product |
| `docs/features/*.md` | After sprint completion | Sprint lead |
| `docs/README.md` | Quarterly | Maintainer |

## When to Update Documentation

Update documentation when:
- Adding new API endpoints or changing existing ones
- Modifying configuration options or defaults
- Changing deployment procedures
- Completing sprint features
- Fixing bugs that affect documented behavior

## Staleness Indicators

Documentation may be stale if:
- `Last Updated` date is older than 90 days for active files
- Code examples reference deprecated APIs
- Version numbers don't match current release
- Links are broken

## Archive Policy

Move documentation to `docs/archive/` when:
- A bug investigation is complete and resolved
- A feature is deprecated or removed
- Historical reference value exists but active maintenance isn't needed

See [docs/archive/README.md](archive/README.md) for archived documentation.

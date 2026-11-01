# REST API Downloader — TODO

**Goal:** A high-performance, safe URL-download service — content negotiation,
stateless-first ethical crawling, browser rendering, batch/scheduled jobs, and
optional multi-tenant quotas.
**Updated:** 2026-11-01

<!-- Counters: doc=5 be=5 test=1 -->

Live status lives ONLY here. States: `- [ ]` open · `- [x]` done · `- [!]`
attempted-but-failed. `spec/` is status-free (the WHAT); `docs/` is planning/ops.

---

## Sprint 0 — Triage (always active)

_No open critical issues._

Recently resolved (usability fixes — substance archived in `DECISIONS.md`):
- [x] **S0-doc-1** Document schedule CRUD endpoints in `README.md` + `spec/contracts.md`.
- [x] **S0-doc-2** Document metrics endpoints (`/metrics`, `/metrics/performance`,
  `/metrics/health-score`) in `README.md` + `spec/contracts.md`.
- [x] **S0-doc-3** Document implicit Playwright fallback in batch processing (`spec/contracts.md`).
- [x] **S0-doc-4** Add Getting Started section — minimal (no Redis/Playwright) vs full setup (`README.md`).
- [x] **S0-doc-5** Document rate-limit storage fallback chain
  (`RATELIMIT_STORAGE_URI → REDIS_URI → memory://`) in `.env.example` + `docs/guides/configuration.md`.
- [x] **S0-be-1** Return clear 503 when Redis is unavailable on batch/schedule endpoints,
  naming the feature that needs Redis.
- [x] **S0-be-2** Fix ErrorResponse nesting inconsistency; OpenAPI schema now matches
  actual `detail` wrapping across routes.

---

## Sprint 4 — Ethical Crawling & Stateless Safety (CURRENT)

Stateless-first `robots.txt` compliance, configurable `User-Agent`, and per-domain
`Crawl-delay` egress protection — working out-of-the-box without Redis in stateless
Kubernetes deployments, with optional Redis coordination when `REDIS_URI` is set.

### Ethical Crawling Infrastructure (P1 — Core)
- [ ] **S4-be-1** Configurable `USER_AGENT` (default: `REST-API-Downloader/1.0`) &
  `RESPECT_ROBOTS_TXT` (`default: True`) settings in `src/downloader/config.py`,
  propagated consistently to `HTTPClient` (`src/downloader/http_client.py`) and
  Playwright browser contexts (`src/downloader/browser/`, `src/downloader/pdf_generator.py`).
- [ ] **S4-be-2** `robots.txt` fetcher & `urllib.robotparser` evaluator
  (`src/downloader/crawling/robots.py`) with **in-memory bounded TTL/LRU cache by
  default** (TTL 1h, working out-of-the-box when `REDIS_URI` is unset) and optional
  Redis cache backend when `REDIS_URI` is configured; fail-open on unreachable
  `robots.txt` with structured warning log.
- [ ] **S4-be-3** Per-domain `Crawl-delay` throttling (`src/downloader/crawling/throttle.py`,
  in-memory per-domain timestamps by default with optional Redis coordination when
  `REDIS_URI` is set) enforced before single and batch downloads.
- [ ] **S4-be-4** Wire `robots.txt` & `Crawl-delay` checks into `GET /{url}`
  (`src/downloader/routes/download.py`) and `POST /batch`
  (`src/downloader/routes/batch.py`), allowing per-request or config opt-out only
  when `RESPECT_ROBOTS_TXT=false`.
- [ ] **S4-be-5** Return `HTTP 403` with clear disallow reason (`ErrorResponse`
  `detail` envelope on `GET /{url}` and per-URL failure reason in `/batch` results)
  plus Prometheus/live metrics counters for `robots.txt` evaluations and cache hits.

### Testing (P1 — Required)
- [ ] **S4-test-1** Unit + mocked API tests (`tests/unit/test_robots.py`,
  `tests/api/test_ethical_crawling.py`) covering allow/disallow `403`, `Crawl-delay`
  pacing, unreachable `robots.txt` fail-open fallback, and memory-vs-Redis cache
  parity (100% offline-sandbox safe).

### Documentation (P1 — Required)
- [ ] **S4-doc-1** Document ethical crawling settings (`USER_AGENT`,
  `RESPECT_ROBOTS_TXT`, cache TTL/backends) and `403` behavior in `README.md` and
  `docs/guides/configuration.md`, and propose `spec/contracts.md` updates for review
  (`spec/` is protected).

**Acceptance criteria (T2 verification gate):**
- `GET /{url}` and `POST /batch` evaluate `robots.txt` and `Crawl-delay` with zero Redis dependency when `REDIS_URI` is unset.
- Disallowed URLs return `HTTP 403` with a clear `detail` reason naming the disallow rule / `User-Agent`.
- Unreachable or malformed `robots.txt` fails open with a structured warning log rather than blocking downloads.
- Bounded in-memory TTL/LRU cache and optional Redis backend pass identical contract tests offline.
- Documentation updated and `spec/contracts.md` diff proposed for review without unprompted edits to `spec/`.

---

## Sprint 5 Preview — Multi-Tenant Quotas (Deferred until multi-key or Redis-backed deployment)

Per-API-key quotas, usage reporting, and `X-RateLimit-*` accounting for multi-tenant
deployments. **Deferred:** single-key stateless Kubernetes deployments without
`REDIS_URI` already enforce endpoint rate limits via `slowapi` (`memory://`) and do
not need Redis-backed tenant accounting. If activated later for multi-key tenancy,
quota storage should either target a Redis-backed deployment or support a
`QUOTA_STORAGE_URI → REDIS_URI → memory://` fallback chain matching `slowapi` parity.

| Task ID | Description | Effort |
|---------|-------------|--------|
| S5-be-1 | `QuotaConfig` model (`src/downloader/quota/models.py`): daily/monthly request & bandwidth limits | 3h |
| S5-be-2 | Per-API-key request counter (`src/downloader/quota/counter.py`) with Redis `INCR`+TTL and `memory://` parity | 4h |
| S5-be-3 | Quota enforcement middleware (`src/downloader/quota/middleware.py`) injecting `X-RateLimit-{Limit,Remaining,Reset}` (<10ms overhead) | 4h |
| S5-be-4 | Usage tracking endpoint (`GET /usage` in `src/downloader/routes/usage.py`): used/remaining, reset timestamp, bandwidth | 3h |
| S5-be-5 | `HTTP 429` quota-exceeded responses with `Retry-After` header and `/usage` pointer | 2h |
| S5-test-1 | Quota unit + integration tests (`tests/unit/test_quota.py`, `tests/integration/test_quota.py`) across `memory://` and Redis | 3h |
| S5-doc-1 | Document quota settings and `X-RateLimit-*` headers in `docs/guides/deployment.md`; propose `/usage` for `spec/contracts.md` | 2h |

---

## Sprint 6 Preview — Extended Features

Candidates (to be prioritized): screenshot/PDF query toggle (`?output=screenshot|pdf`),
webhook notifications on job completion, OCR (image→text), SDK/client libraries.

---

## Completed Sprints

| Sprint | Theme | Archive |
|--------|-------|---------|
| S0 | Production Readiness | [docs/features/production-readiness.md](docs/features/production-readiness.md) |
| S1 | Content Transformation | [docs/features/content-transformation.md](docs/features/content-transformation.md) |
| S2 | Browser Rendering | [docs/features/browser-rendering.md](docs/features/browser-rendering.md) |
| S3 | Scheduling API | [docs/features/scheduling.md](docs/features/scheduling.md) |

Decisions log: [DECISIONS.md](DECISIONS.md) · Requirements: `spec/` · Roadmap: [docs/roadmap.md](docs/roadmap.md).

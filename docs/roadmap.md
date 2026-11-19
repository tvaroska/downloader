# REST API Downloader — Roadmap

The ordered list of upcoming work. Each milestone ends in a tagged release; there is no `TODO.md`. Hard invariants live in [`CRITICAL.md`](../CRITICAL.md), architectural rationale in [`DECISIONS.md`](../DECISIONS.md), public wire contracts in [`spec/contracts.md`](../spec/contracts.md), capability baselines in [`features/`](features/), and implemented release history in [`archive/roadmap-history.md`](archive/roadmap-history.md) (move completed milestones or `- [x]` items with `proj-test roadmap-archive downloader --done`, verified via `proj-test roadmap-archive downloader --check`).

## Where we are

`v0.8.3` (`3fb4121`, covering Milestones 2–5 / `R1`–`R114`) is live at `https://download.tvaroska.sk` across two stateless regional GKE Autopilot replicas (`ENVIRONMENT=production`, `PDF_POOL_SIZE=1`, `PDF_CONCURRENCY=1`, single `DOWNLOADER_KEY`, no `REDIS_URI`). Only the two deferred `v0.9.0+` feature tracks remain in [`## Unscheduled backlog`](#unscheduled-backlog).

## Milestones at a glance

| # | Milestone | Done when | Size / Target |
| :--- | :--- | :--- | :--- |
| Next | `v0.9.0+` — Promote selected items from [`## Unscheduled backlog`](#unscheduled-backlog) | Multi-key quota accounting or stateless Streamable HTTP MCP `2026-07-28` demand triggers scheduling | Unscheduled |

## Unscheduled backlog

Items below have no target release and use plain bullets rather than `- [ ]` checkboxes so `work_sync.py` counts only scheduled milestone tasks (`0` open). Promote an item into a milestone (`- [ ]`) when scheduling it.

### Features

- **Multi-Tenant Per-Key Quotas (`v0.9.0+`, ~21h across `S5-be-1`..`S5-be-5`, `S5-test-1`, `S5-doc-1`):** `QuotaConfig`, per-API-key request and bandwidth counters with `QUOTA_STORAGE_URI -> REDIS_URI -> memory://` storage parity, enforcement middleware (`X-RateLimit-{Limit,Remaining,Reset}`), `GET /usage`, `HTTP 429` with `Retry-After`, and tier-based plans. Deferred until multi-key or Redis-backed multi-tenant deployment is required.
- **Stateless MCP `2026-07-28` Adapter:** Expose extraction, Playwright `render_pdf`, and `/batch` crawls over stateless `2026-07-28` Streamable HTTP MCP using `SEP-2663` Tasks (`io.modelcontextprotocol/tasks`) for non-blocking async jobs, returning extracted Markdown/PDF outputs as `downloader://jobs/{id}/article.md` MCP Resources instead of inlining large page bodies into tool responses.

## Shipped

- **v0.3.0 to v0.5.0 (Sprints 0–3, 2026-01):** Core HTTP/batch foundation, SSRF & production readiness, HTML-to-Markdown & plain-text `Accept` content negotiation, lazy shared Playwright `BrowserPool` (`?render=true`, `?wait_for=`), and Redis cron `/schedules` CRUD & execution history — [archive](archive/roadmap-history.md#v030-to-v050-content-transformation-browser-rendering-and-scheduling)
- **v0.6.0 (Sprint 4, 2026-11-01):** Stateless-first `robots.txt` compliance & `Crawl-delay` throttling, streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`), redirect & Playwright SSRF route guards, active `RateLimitMiddleware` (`HTTP 429`), browser recycling & idle scale-to-zero, JSON access logs, and `:8000` non-root container — [archive](archive/roadmap-history.md#v060-sprint-4-ethical-crawling-and-production-hardening)
- **v0.6.1 (2026-11-02):** DNS-pinned `SSRFSafeHTTPTransport` (`trust_env=False`), single-fetch Playwright pipeline under unified `pdf_semaphore`, `/metrics*` API-key auth & production OpenAPI `404`, right-to-left `RATELIMIT_TRUSTED_PROXIES` XFF resolution, and atomic Lua `DomainThrottle` + stateful `/batch` & `/schedules` parity — [archive](archive/roadmap-history.md#v061-security-single-fetch-browser-and-stateful-pipeline-hardening)
- **v0.6.2 (`S6-br-1` / `F-2026-11-05-002`, 2026-11-12):** Single-pass `domcontentloaded` navigation with bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`), eliminating `10s–20s` timeouts on tracker-heavy origins — [archive](archive/roadmap-history.md#v062-playwright-navigation-timeout-resilience-f-2026-11-05-002)
- **v0.7.0 (Extended Features & Operations, 2026-11-12):** Screenshot/PDF toggles, allowlisted declarative Playwright `actions` (`POST /render`), post-persistence SSRF-safe HMAC-SHA256 webhooks, Tesseract OCR, typed Python/JS/Go SDKs, graceful connection drain (`drain.py`), split API+worker Redis `BLMOVE` queue (`DEPLOYMENT_MODE`), and opt-in OpenTelemetry tracing — [archive](archive/roadmap-history.md#v070-extended-features-and-operations)
- **Milestone 1 — `v0.7.1`: Live Prod Safety, Ops & Fast Gates:** All 3 live GKE production findings, stateless production-path `P0`s (`R1`–`R6`), and fast auth/isolation/SDK gates (`R15`, `R16`, `R24`, `R26`) pass offline `proj-test downloader` + Cloud Build, and `v0.7.1` (including the `main` Trivy/CVE upgrades & GHCR mirror) is live on GKE — [archive](archive/roadmap-history.md#milestone-1---v071-live-prod-safety-ops-fast-gates)
- **Milestone 2 — `v0.8.0`: Ports, Adapters and Contracts & Strict Gates (shipped in `v0.8.3`, 2026-11-19):** BrowserSession/BrowserRenderer/EgressPolicy, single `convert()` core, and JobStore/WorkQueue ports (`R44`–`R50`), ruff BLE/C90/PLR, `--cov=src/downloader` (`>=83%`), and `mypy src/ tests/` (`R54`–`R62`, `R72`) enforced in the gate — [archive](archive/roadmap-history.md#milestone-2---v080-ports-adapters-and-contracts-strict-gates)
- **Completed (shipped in `v0.8.3`, 2026-11-19):** Milestone 3 — `v0.8.1`: Observability, SDK Parity & Test Suite Cleanup; Milestone 4 — `v0.8.2`: Stateful Redis/Worker & Wire Contract Parity — [archive](archive/roadmap-history.md#completed-2026-11-18)
- **Milestone 5 — `v0.8.3`: Release Blockers, Pure Ports Split & Redis/Worker Hardening (`R77`–`R114`, shipped 2026-11-19):** Playwright/pool fallback translation (`R77`), UTF-8 rendered HTML charset labeling (`R78`), enforced `83%` branch coverage + `ast-grep-cli` + Playwright CI tier (`R80`–`R82`), UNIX cron expansion & `OrTrigger` (`R79`), draining worker heartbeat/atomic `complete_job()`/Lua reclaim/remote parity (`R83`–`R89`), per-download redirect cookies (`R90`), transitive zero-framework Ports split (`R95`), sealed webhook secrets (`R113`), and full spec/SDK alignment (`R91`–`R114`) — [archive](archive/roadmap-history.md#milestone-5---v083-release-blockers-pure-ports-split-and-redisworker-hardening-r77-r114)

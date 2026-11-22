# REST API Downloader — Roadmap

The ordered list of upcoming work. Each milestone ends in a tagged release; there is no `TODO.md`. Hard invariants live in [`CRITICAL.md`](../CRITICAL.md), architectural rationale in [`DECISIONS.md`](../DECISIONS.md), public wire contracts in [`spec/contracts.md`](../spec/contracts.md), capability baselines in [`features/`](features/), and implemented release history in [`archive/roadmap-history.md`](archive/roadmap-history.md) (move completed milestones or `- [x]` items with `proj-test roadmap-archive downloader --done`, verified via `proj-test roadmap-archive downloader --check`).

## Where we are

`v0.8.3` (`3fb4121`, covering Milestones 2–5 / `R1`–`R114`) is live at `https://download.tvaroska.sk` across two stateless regional GKE Autopilot replicas (`ENVIRONMENT=production`, `PDF_POOL_SIZE=1`, `PDF_CONCURRENCY=1`, single `DOWNLOADER_KEY`, no `REDIS_URI`). Next up is Milestone 6 (`v0.8.4`: modal-closer & session teardown reliability fix for `F-2026-11-20-001`); the two deferred `v0.9.0+` feature tracks remain in [`## Unscheduled backlog`](#unscheduled-backlog).

## Milestones at a glance

| # | Milestone | Done when | Size / Target |
| :--- | :--- | :--- | :--- |
| 6 | `v0.8.4` — Modal-Closer & Teardown Reliability | `close_page_modals()` skips hidden buttons and uses a monotonic deadline instead of `asyncio.timeout`, `BrowserSession._cleanup()` unroutes/cancels in-flight tasks before `ctx.close()`, and `0` `Future exception was never retrieved` errors occur in production | `v0.8.4` (~0.25d) |
| 7 | `v0.8.5` — Loopback E2E CI & Escape-Analysis Gate | Deterministic HTTP → Playwright loopback E2E scenarios run in `cloudbuild.yaml` against `downloader:ci` (covering modal dismissal, teardown, PDF/Markdown negotiation, and SSRF) and `AGENTS.md` enforces E2E/prod → unit escape analysis | `v0.8.5` (~0.6d) |
| Next | `v0.9.0+` — Promote selected items from [`## Unscheduled backlog`](#unscheduled-backlog) | Multi-key quota accounting or stateless Streamable HTTP MCP `2026-07-28` demand triggers scheduling | Unscheduled |

## Milestone 6 — `v0.8.4`: Modal-Closer & Teardown Reliability

- [ ] **R115 (P2, 0.25d): Eliminate Playwright `Future exception was never retrieved` (`TargetClosedError`) in modal dismissal and session teardown (`F-2026-11-20-001`)**
  - **Files:** [`session.py`](../src/downloader/browser/session.py) (`close_page_modals`, `_make_ssrf_route_guard`, `BrowserSession._cleanup`), unit tests in `tests/unit/`
  - **Root cause (verified from live logs):** `close_page_modals()` (`session.py:309-341`) wraps `await button.click(timeout=1000)` inside `async with asyncio.timeout(timeout_seconds):`. When invisible matching close buttons exhaust the `3.0s` budget mid-click, `asyncio.timeout` cancels Playwright's `Channel._inner_send` `asyncio.wait(...)` before `callback.future.cancel()` runs, leaving an orphaned Playwright RPC `Future` that rejects with `TargetClosedError` (`attempting click action`, `element is not visible`) when `BrowserSession._cleanup()` (`session.py:536-569`) calls `ctx.close()`.
  - **Fix:** (1) In `close_page_modals()`, drop `asyncio.timeout` around Playwright RPCs; track `deadline = time.monotonic() + timeout_seconds`, skip buttons where `await button.is_visible()` is `False`, clamp `button.click(timeout=min(1000, remaining_ms))` and `page.wait_for_timeout(min(200, remaining_ms))` to remaining budget, and return cleanly once `remaining_ms <= 0`. (2) Defensively in `BrowserSession._cleanup()`, call `await ctx.unroute_all(behavior="ignoreErrors")` (when exposed) and cancel/drain `ctx._ssrf_redirect_tasks` before `ctx.close()`, and catch `PlaywrightError` around `route.continue_()` / `route.abort()` inside `_make_ssrf_route_guard` so closing a context mid-DNS lookup never leaks an unhandled route task exception.
  - **Accept:** Unit tests cover hidden modal buttons skipped without burning click timeouts, budget exhaustion exiting cleanly without cancelling an in-flight Playwright RPC future, and `_cleanup()` unrouting/draining pending redirect tasks before `ctx.close()`; `proj-test downloader` passes.

## Milestone 7 — `v0.8.5`: Loopback E2E CI & Escape-Analysis Gate

- [ ] **R116 (P1, 0.5d): Wire deterministic loopback E2E scenarios into `cloudbuild.yaml`**
  - **Files:** [`cloudbuild.yaml`](../cloudbuild.yaml), [`tests/e2e/test_html_rendering_e2e.py`](../tests/e2e/test_html_rendering_e2e.py), [`tests/e2e/test_examples.py`](../tests/e2e/test_examples.py), `tests/e2e/test_loopback_e2e.py` (new)
  - **Scope:** Today `tests/e2e/` (`14` tests) is excluded from both `scripts/gate.sh` and `cloudbuild.yaml`, leaving only 1 real-Chromium SSRF redirect test and an inline PDF smoke in CI. Add a deterministic, zero-internet loopback HTTP fixture server inside `downloader:ci` exercising live `GET /{url}` (HTML, Markdown, PDF, hidden-modal dismissal + clean context teardown with zero `Future exception was never retrieved` warnings) and `POST /render` declarative actions, and wire it as a blocking Cloud Build step (keeping live-internet `@pytest.mark.network` tests opt-in).
  - **Accept:** New Cloud Build E2E step runs real HTTP → FastAPI → Playwright Chromium scenarios against `downloader:ci` in no-Redis production mode and fails on any unhandled asyncio/Playwright exception.
- [ ] **R117 (P2, 0.1d): Codify E2E/prod → unit escape-analysis rule in `AGENTS.md`**
  - **Files:** [`AGENTS.md`](../AGENTS.md)
  - **Scope:** Require every E2E, integration, or production finding (e.g. `F-2026-11-20-001`) to record why offline unit/fake suites missed it (`missing_unit_case | fake_drift | env_parity`) and land a failing-first unit or loopback-Chromium regression test before closing.
  - **Accept:** `AGENTS.md` documents the escape-analysis checklist under Testing Strategy; `gate test downloader` passes.

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

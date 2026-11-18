# REST API Downloader - Strategic Roadmap

**Last Updated:** 2026-11-18

## Overview

This document outlines the strategic priorities, completed releases, and deferred backlog for the REST API Downloader service. Milestones are aligned with stateless-first Kubernetes operations (`REDIS_URI` optional for single-URL extraction, PDF rendering, rate limiting, and ethical crawling; required only for `/batch` and `/schedules`). Architectural rationale lives in [DECISIONS.md](../DECISIONS.md) and public wire contracts live in [spec/contracts.md](../spec/contracts.md).

---

## Current Status

| Phase | Status | Completion |
|-------|--------|------------|
| Phase 1: Core Functionality | ✅ Complete | 100% |
| Phase 2: Batch Processing | ✅ Complete | 100% |
| Phase 3: Performance & Reliability | ✅ Complete | 100% |
| Phase 4: Security & Production | ✅ Complete | 100% |
| Phase 5: Content Transformation | ✅ Complete (`v0.7.0` — OCR delivered) | 100% |
| Phase 6: Browser Rendering | ✅ Complete (`v0.7.0` — Screenshots, actions & `v0.6.2` timeout fix) | 100% |
| Phase 7: Ethical Crawling & Stateless Safety | ✅ Complete (`v0.6.0` — Sprint 4) | 100% |
| Phase 8: Scheduling & Multi-Tenant Quotas | 🚧 Partial (Scheduling & Webhooks ✅; Quotas deferred past `v0.7.0`) | 60% |

---

## Current Focus

### Completed: Release `v0.6.2` — Playwright Navigation Timeout Resilience (`F-2026-11-05-002`) ✅

**Release `v0.6.2`** (Complete — 2026-11-12 — P1 Reliability):
- [x] **S6-br-1 (P1 — Single-Pass `domcontentloaded` + Bounded Network Settle in `src/downloader/content_converter.py`):** Replaced the two-pass `networkidle` retry pattern in `convert_content_with_playwright_fallback` and `render_html_with_playwright` with a single `page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)` followed by a bounded non-fatal settle (`page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 2500))`), eliminating `10s–20s` navigation timeouts on ad- and tracker-heavy origins (`F-2026-11-05-002`), verified by `tests/unit/test_v062_playwright_timeout.py`.

### Completed: Release `v0.7.0` — Extended Features & Operations ✅

**Release `v0.7.0`** (Complete — 2026-11-12 — Extended Features & Operations/Infrastructure):
- **Extended Features (5/5 Complete):**
  - Screenshot/PDF output toggle (`?output=screenshot|pdf`, `?full_page=bool`, `?screenshot_format=png|jpeg`, `Accept: image/png` / `image/jpeg`, `POST /render`, and `/batch` + `/schedules` `"screenshot"` format)
  - SSRF-safe HMAC-SHA256 webhook notifications (`src/downloader/services/webhooks.py`) firing post-persistence on batch completion/failure and schedule execution (`X-Downloader-Signature-256`, `trust_env=False`, `follow_redirects=False`, creation-time SSRF validation, secret redaction)
  - Image-to-text OCR (`src/downloader/transformers/ocr.py` via `pytesseract` + `Pillow` + `tesseract-ocr`, decompression-bomb pixel cap, language validation, `HTTP 503` `ocr_unavailable` graceful degradation)
  - Typed client SDKs for Python (`sdks/python`), JavaScript/TypeScript (`sdks/javascript`), and Go (`sdks/go`) with automated test suites
  - Declarative allowlisted browser interaction scripting (`src/downloader/browser/actions.py`: 8 action types, max 20 steps, zero raw JS evaluation) across `GET /{url}?actions=`, `POST /render`, and `POST /batch`
- **Operations & Infrastructure (3/3 Complete):**
  - Graceful shutdown with active-request and background-job connection drain (`src/downloader/drain.py`: `DrainCoordinator`, `DrainMiddleware`, `HTTP 503 service_draining` + `Connection: close` + `Retry-After: 5`, `/health` draining state, uvicorn `--timeout-graceful-shutdown 35`)
  - Split Worker Architecture (`DEPLOYMENT_MODE=all|api|worker`, `src/downloader/worker_queue.py` reliable Redis `BLMOVE` queue + browser RPC + heartbeats, `src/downloader/worker.py` with `--healthcheck`, `docker-compose.split.yml`)
  - Opt-in OpenTelemetry distributed tracing (`src/downloader/telemetry.py`, `OTEL_ENABLED=false` default, OTLP HTTP/console exporters, FastAPI/httpx/Redis/Playwright/OCR/webhook/worker spans, `trace_id`/`span_id` log correlation, `X-Trace-ID` response header, W3C `traceparent` propagation)

### Completed: Release `v0.6.1` — Security, Single-Fetch Browser & Stateful Pipeline Hardening ✅

**Release `v0.6.1`** (Complete — 2026-11-02): Defense-in-Depth SSRF Pinning, Single-Fetch Playwright Pipeline & Stateful Parity
- **Track A (Browser & Single-Fetch Pipeline):** Eliminated redundant pre-flight HTTP fetches on `Accept: application/pdf` and `?render=true` / `?wait_for=` across `text/html`, `text/plain`, `text/markdown`, and multi-format requests; unified all Chromium pathways under `pdf_semaphore` (`503` fail-fast on explicit `?render=true`/PDF, graceful static degradation on auto-detect/empty-body fallback); removed the unconditional `2s` sleep in `generate_pdf`; wired `ContentConfig` flags and monotonic-TTL `BoundedCache`
- **Track B (Auth, SSRF Pinning & Rate Limits):** Restored `.env` loading in `AuthConfig` with `secrets.compare_digest` verification; required API key auth on `/metrics*` when `DOWNLOADER_KEY` is set and disabled `/docs`, `/redoc`, and `/openapi.json` (`HTTP 404`) in `ENVIRONMENT=production`; implemented `SSRFSafeNetworkBackend` / `SSRFSafeHTTPTransport` (`trust_env=False`, DNS-pinned `connect_tcp` preventing DNS-rebinding TOCTOU, non-blocking `asyncio.to_thread` DNS resolution, and hex/octal/int/mapped-IPv6/`*.localhost` blocks); added right-to-left `RATELIMIT_TRUSTED_PROXIES` `X-Forwarded-For` client IP resolution and auth vs unauth bucket isolation
- **Track C & D (Stateful `/batch`, `/schedules` & Redis Atomicity):** Added creation-time SSRF & custom header sanitization (`HTTP 400`) on `POST /schedules` and `POST /batch`, `robots.txt` + `DomainThrottle` + non-retryable policy short-circuit + per-attempt `SCHEDULER_MAX_WORKERS` semaphore + JSON `content_size` accounting in `ScheduledJobExecutor`, atomic Lua `DomainThrottle` Redis slot reservation, `(scheme, netloc)` `robots:v2:{scheme}:{netloc}` cache isolation, and incremental `/batch` progress updates

### Completed: Sprint 4 — Ethical Crawling & Production Hardening (`v0.6.0`) ✅

**Sprint 4** (Complete — 2026-11-01): Ethical Crawling, Egress Protection & Defense-in-Depth Hardening
- 7 tasks completed (`S4-be-1`..`S4-be-5`, `S4-test-1`, `S4-doc-1`)
- Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`) across `HTTPClient` and Playwright
- Stateless-first `robots.txt` evaluation (`urllib.robotparser`) with bounded in-memory TTL/LRU cache by default (TTL 1h, zero Redis dependency), optional Redis backend when `REDIS_URI` is configured, and cached fail-open sentinels on unreachable origins
- Per-domain `Crawl-delay` throttling (in-memory timestamps by default + optional Redis coordination), `HTTP 403` `robots_disallowed` responses, and Prometheus `robots_*` counters
- Production hardening: streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413` `content_too_large`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After` + `X-RateLimit-*`), `BrowserPool` max-use recycling (`PDF_MAX_USES_PER_BROWSER=50`) & idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`), structured JSON `uvicorn.access` `path`/`method`/`status_code` fields, and unprivileged container port `:8000`
- See [docs/features/ethical-crawling.md](features/ethical-crawling.md)

### Deferred Preview (`v0.7.1` Reliability & Post-`v0.7.0` / `v0.8.0+` Multi-Tenant Quotas)

- [ ] **P1 (`v0.7.1` — Binary `Content-Type` Guard Against `OOMKilled` on Text/Multi-Format Routes, `services` `F-2026-11-16-002`):** Guard text, HTML, markdown, and multi-format conversion paths (`src/downloader/http_client.py:190-197,245-274`, `src/downloader/services/content_processor.py:626-627`, and `src/downloader/services/multi_format.py:133-150`) so non-text binary `Content-Type`s (`audio/*`, `video/*`, `application/octet-stream`, archives) are rejected early (`HTTP 415` / structured format error) before buffering up to `50 MB` and running `.decode()` + `json.dumps()` across parallel format tasks. *Review 2026-11-18:* the location list is incomplete. The default text and markdown path decodes any non-HTML body and returns it as text (`src/downloader/content_converter.py:684-692`); also `src/downloader/services/content_processor.py:563`, `src/downloader/services/content_negotiation.py:104-124` (base64 plus `json.dumps` of the full body) and `src/downloader/services/batch_processor.py:270,283-294`. A Content-Type reject does not cover compressed responses (`R3`) or the per-job result total (`R43`).
- [ ] **P3 (`v0.7.1` — Auto-Detected Playwright Render Timeouts & Duplicate `ERROR` Logs, `services` `F-2026-11-17-002`):** Three article URLs (`https://www.dwarkesh.com/p/paul-christiano-2`, `https://www.decodingai.com/p/build-durable-secure-long-running-agents`, `https://newsletter.pragmaticengineer.com/p/inference-engineering-for-software-engineers`) hit `Page.goto: Timeout 10000ms exceeded` in `render_html_with_playwright` (`src/downloader/content_converter.py:134`, `html_js_rendering_timeout` in `src/downloader/config.py:264-269`), logged the failure twice at `ERROR` (`content_converter.py:579` plus `src/downloader/services/content_processor.py:552` or `:613-615`), then served the static HTML. (1) Reproduce the URLs and record which `should_use_playwright_for_html` heuristic (`content_converter.py:284-382`) sends them to the browser and why navigation stalls; neither is confirmed yet. One candidate: the JS-required phrase match runs on full-document text including `<noscript>` (`:328-341`) before the substantial-static-content check (`:372-375`). (2) Fix the confirmed cause so server-rendered pages with substantial body text skip the browser (for the candidate above: drop `<noscript>`/`<script>`/`<style>` text before the phrase match and run the static-content check first). (3) Log an auto-detected render failure that falls back to static HTML once at `WARNING`; keep `ERROR` for explicit `?render=true` failures. Leave the `10s` timeout alone unless the reproduction shows a page needs longer. Accept: the URLs return without a browser timeout, a degraded auto-render emits one `WARNING` and no `ERROR`, and `proj-test downloader` passes. *Review 2026-11-18:* measured on synthetic pages, not on the three URLs. The `<noscript>` candidate is confirmed, and two more causes send static pages to the browser: the phrase `"enable js"` (`src/downloader/content_converter.py:332`) matches ordinary prose such as "enable JSON mode", and `content_size < 50000 and _has_missing_metadata` (`:358`) renders every small static page without Open Graph or Twitter tags.
- [ ] **P3 (`v0.7.1` — Log Upstream `4xx` Client Errors at `WARNING`, `services` `F-2026-11-18-001`):** In `src/downloader/routes/download.py:358-365`, log upstream `400 <= e.status_code < 500` `HTTPClientError`s at `logger.warning` instead of `logger.error` (matching `PDFGeneratorError` at `:375-376`, while keeping `logger.error` for `5xx` and network/timeout errors).

**Post-`v0.7.0` Backlog** (Deferred until multi-key or Redis-backed multi-tenant deployment): Multi-Tenant Per-Key Quotas
- 7 tasks (`S5-be-1`..`S5-be-5`, `S5-test-1`, `S5-doc-1`), ~21 hours effort
- `QuotaConfig`, per-key request/bandwidth counters, `X-RateLimit-{Limit,Remaining,Reset}` headers, `GET /usage`, and `HTTP 429` with `Retry-After`
- Deferred past `v0.7.0` because single-key stateless Kubernetes deployments without `REDIS_URI` do not need Redis-backed tenant accounting (`slowapi` `memory://` already enforces global endpoint rate limits); if activated later, quota storage should support `QUOTA_STORAGE_URI → REDIS_URI → memory://` parity

**Post-`v0.7.0` Backlog — Stateless MCP `2026-07-28` Adapter**
- Expose extraction, Playwright `render_pdf`, and `/batch` crawls over stateless `2026-07-28` Streamable HTTP MCP using `SEP-2663` Tasks (`io.modelcontextprotocol/tasks`) for non-blocking async jobs
- Return extracted markdown/PDF outputs as `downloader://jobs/{id}/article.md` MCP Resources instead of inlining large page bodies into tool responses

### Completed: Sprint 3 — Scheduling API ✅

**Sprint 3** (Complete): Scheduling CRUD API
- 7 tasks completed
- Cron-based scheduling, job execution with retry, paginated execution history
- See [docs/features/scheduling.md](features/scheduling.md)

---

## Milestone `v0.7.1` — Review Findings (2026-11-18)

Architecture, code-quality and test review of `main` at `d3452cd`. All 76 items (`R1`…`R76`) are scheduled in `v0.7.1`, the next milestone, together with the three `v0.7.1` items under Deferred Preview above. No `src/` or `sdks/` change exists after the `v0.7.0` tag, so every item is older debt or shipped in `v0.7.0`.

- **Severity:** `P0` wrong output, broken security control or data loss · `P1` bugs on error and load paths, missing invariant tests, gate drift · `P2` Ports, Adapters and Contracts boundaries, contract drift, refactor-blocking coupling · `P3` dead code, duplication, docs · `P4` optional.
- **Evidence:** `[run]` reproduced against the real library or route · `[fake]` reproduced with a fake Redis or fake browser · `[read]` code reading only (no Chromium, Redis or Docker was available). Every `P0` was reproduced or re-read independently by a second reviewer; none was refuted.
- **Exposure:** production runs one pod, one API key and no `REDIS_URI` ([DECISIONS.md](../DECISIONS.md)). Items tagged `(Redis)` or `(split)` cannot occur there today.
- **Mutation check:** 45 one-line mutants in a scratch copy; 15 killed, 30 survived the offline gate. Survivors are named in the items below.
- **Suggested order inside `v0.7.1`:** `R1`–`R6` first (production path), next to the Content-Type guard; `R7`–`R13` before any deployment sets `REDIS_URI` or `DEPLOYMENT_MODE=api|worker`; `R15`, `R16` and `R24` are test-only and small (`R24` adds 104 tests to the gate for about 2 s); then the rest of `P1`, `P2`, `P3`, `P4` in that order.

### P0 — on the production path

- [ ] **R1 · Browser SSRF guard is not applied to redirect hops** `[read]`. `src/downloader/pdf_generator.py:61-90,245-252`; `src/downloader/content_converter.py:426-427,524-525`; `src/downloader/browser/manager.py:387-397`. `context.route("**/*", …)` is the only egress control on the PDF, screenshot and render paths, and the bundled Playwright driver continues redirected requests without calling route handlers (`crNetworkManager.js`, `redirectedFrom` branch). A public URL that answers `302` to an internal address is rendered and returned; nothing re-validates the final URL, and contexts allow service workers. Not run in Chromium. `docs/features/ethical-crawling.md:37`, `DECISIONS.md:127-129` and `README.md:9` state the opposite. **Next:** after navigation re-validate every URL in `response.request.redirected_from` and `page.url` and fail the render; create contexts with `service_workers="block"`; then enforce at connect time (Chromium behind an in-process pinning proxy) and correct the three statements. **Verify:** add `tests/integration/test_browser_ssrf_redirect.py::test_redirect_hop_to_denied_target_is_not_rendered` (`requires_playwright`); `git grep -n -E "redirected_from|service_workers" -- src` is empty today.
- [ ] **R2 · Target query string is dropped** `[run]` (server, Python SDK), `[read]` (JS, Go). `src/downloader/routes/download.py:73,112`; `sdks/python/src/downloader_sdk/client.py:113`; `sdks/javascript/src/index.ts:274,289-290`; `client.go:144-147` in `sdks/go`. `GET /{url}` takes the target from the path only and the SDKs append it unencoded, so `…/search?q=test&page=2` is fetched as `…/search` and returned with `200`. With options set, JS and Go build a URL with two `?`. **Next:** SDKs percent-encode `%`, `?` and `#` in the target; the server answers `400 validation_error` for query keys outside the documented set; propose a spec paragraph on encoding. **Verify:** add `tests/api/test_download.py::test_target_query_string_reaches_http_client` and `sdks/python/tests/test_client.py::test_download_encodes_target_query` (plus JS and Go equivalents).
- [ ] **R3 · Size cap counts bytes after decompression** `[run]`. `src/downloader/http_client.py:168,261-268`; same pattern in `src/downloader/crawling/robots.py:287-292`. A 405-byte brotli response decoded to 256 MiB in one chunk with the cap at 1 MiB, so `CONTENT_MAX_DOWNLOAD_SIZE` is not a memory bound; the robots pre-flight is exposed too. **Next:** read `aiter_raw()` and decode with a bounded output buffer, raising `ContentTooLargeError` when the budget is exceeded; reject other encodings; share the helper with the robots fetch. **Verify:** add `tests/unit/test_http_client_decompression.py::test_brotli_bomb_bounded_by_max_download_size`; `git grep -n -i -E "brotli|decompress" -- tests` is empty today.
- [ ] **R4 · Non-UTF-8 pages silently lose characters** `[run]`. `src/downloader/content_converter.py:686`; `src/downloader/services/multi_format.py:137,150`; `src/downloader/services/content_processor.py:563,608-624`; `src/downloader/services/batch_processor.py:270`. Bodies are decoded as UTF-8 with `errors="ignore"` whatever charset is declared (`café crème` becomes `caf crme`, Cyrillic text disappears), with `200` and no log. Auto-rendered HTML is UTF-8 served under the origin's charset label `[read]`. **Next:** resolve the charset once (header, then detection on the bytes), decode with `errors="replace"`, label browser output `text/html; charset=utf-8`. **Verify:** add `tests/unit/test_charset_handling.py::test_text_and_markdown_respect_declared_charset` and `::test_auto_rendered_html_is_labelled_utf8`; `git grep -n -i -E "windows-125|iso-8859|shift_jis" -- tests` is empty today.
- [ ] **R5 · SSRF blocklist misses `100.64.0.0/10`** `[run]`. `src/downloader/validation.py:260-340` (last check `:334`). `is_private` is false for the shared address space, so those addresses pass on every path (validation, pinned transport, redirects, robots, browser guard). Impact depends on what the pod can reach in that range. **Next:** deny when `not ip_obj.is_global` after the specific checks. **Verify:** add `tests/unit/test_validation_address_classes.py::test_shared_address_space_is_blocked`; `git grep -n is_global -- src` is empty today.
- [ ] **R6 · One cookie jar for all downloads** `[run]`. `src/downloader/http_client.py:158-172`; robots client `src/downloader/crawling/robots.py:266-277`. The process-wide `httpx.AsyncClient` stores `Set-Cookie` and replays it on the next caller's request to that origin, so results depend on earlier requests. **Next:** a jar that stores nothing, or a per-download client over the shared transport. **Verify:** add `tests/unit/test_http_client_isolation.py::test_cookies_do_not_persist_across_downloads`; `git grep -n -i cookie -- tests` is empty today.

### P0 — shipped features that need `REDIS_URI`

- [ ] **R7 · Cron day-of-week fires on the wrong days** `(Redis)` `[run]`. `src/downloader/routes/schedules.py:161`; `src/downloader/models/schedule.py:70`. The fifth field goes to APScheduler unchanged (0 = Monday) while `spec/contracts.md:1534-1548` says 0 = Sunday: `0 9 * * 1-5` runs Tuesday to Saturday. **Next:** translate numeric weekday tokens to names in one helper used by the model validator and the route. **Verify:** add `tests/unit/test_scheduler.py::TestCronExpressionValidation::test_weekday_range_fires_monday_to_friday` on a real `CronTrigger`.
- [ ] **R8 · `GET /schedules` returns a scrambled `cron_expression`** `(Redis)` `[run]`. `src/downloader/routes/schedules.py:117-126` (used at `:253,315`). `_format_cron_trigger` assumes the wrong field order: `30 9 1 6 *` reads back as `6 1 * * 9`. **Next:** store the submitted expression in the job kwargs and return it; delete `_format_cron_trigger`. **Verify:** add `tests/unit/test_scheduler.py::test_list_schedules_round_trips_cron_expression`.
- [ ] **R9 · Stored batch results say `running` with `total_duration: 0.0`** `(Redis)` `[fake]`. `src/downloader/job_manager.py:320-332,369-371`; `src/downloader/worker.py:98-99`. Results are snapshotted before the status becomes `completed`, so `GET /jobs/{id}/results` and `X-Job-Status` report `running` for every finished job. **Next:** pass final status, duration and completion time into `store_job_results`; keep persist-before-webhook. **Verify:** add `tests/test_job_manager.py::TestJobManagerBackgroundLifecycle::test_result_blob_reports_completed_and_duration`.
- [ ] **R10 · Job status has no terminal-state guard** `(Redis)` `[fake]`. `src/downloader/job_manager.py:255,381-384,406-429`; `src/downloader/services/batch_processor.py:511-518`; `src/downloader/worker.py:80,99,105`. In split mode a cancel is overwritten by the worker's next progress update and the job completes; on one instance a cancel during webhook delivery turns a `completed` job into `cancelled` with results unavailable. **Next:** transition table in `update_job_status`; check status between URLs; deliver the webhook outside the cancellable section. **Verify:** add `tests/test_job_manager.py::TestJobManagerBackgroundLifecycle::test_progress_update_does_not_resurrect_cancelled_job` and `::test_cancel_during_webhook_keeps_completed`.
- [ ] **R11 · `JobManager.disconnect()` raises while cancelling running jobs** `(Redis)` `[fake]`. `src/downloader/job_manager.py:137,394-397`; caller `src/downloader/main.py:246-250`. Each task's `finally` deletes from the dict being iterated (`RuntimeError`): only the first job is cancelled, the rest stay `running` for 24 h, the Redis pool is not closed and telemetry is not flushed. Occurs when a batch outlives the drain wait. **Next:** iterate over a copy, gather with `return_exceptions=True`, close the pool in `finally`. **Verify:** add `tests/test_job_manager.py::TestJobManagerConnection::test_disconnect_with_real_background_job` built on `start_background_job`.
- [ ] **R12 · A starting worker takes jobs that live workers are processing** `(split)` `[read]`, requeue shown with one fake queue. `src/downloader/worker.py:76-82,104-117,276`; `src/downloader/worker_queue.py:114-132`. Every worker start moves the whole shared processing list back to pending with no owner or heartbeat check: duplicate runs and webhooks, and a later crash can lose the job. A job still running at drain timeout is marked `cancelled` and acked; there is no attempt counter. **Next:** one processing list per worker, reclaimed only after its heartbeat expires; skip terminal statuses on dequeue; requeue instead of ack on shutdown; attempt count in the envelope. **Verify:** add `tests/unit/test_v070_ops_drain_worker_otel.py::TestSplitWorkerArchitecture::test_second_worker_start_does_not_take_inflight_job` and `::test_shutdown_cancel_requeues_instead_of_acking`.
- [ ] **R13 · Schedules created through the API do not reach an idle worker** `(split)` `[read]`, simulated with two schedulers on one in-memory store. `src/downloader/main.py:179`; `src/downloader/routes/schedules.py:175`; `src/downloader/worker.py:292-293`; `src/downloader/scheduler/service.py:26-40,100-105`. APScheduler wakes only the instance that called `add_job`; the worker sees the job at its next wake-up or restart, and runs missed by more than the 60 s grace are skipped. Nothing stops two active schedulers from each running every schedule (not exercised); `docker-compose.prod.yml:14` stacked on `docker-compose.split.yml:7` produces that. **Next:** a periodic worker wake-up shorter than the grace (or a Redis publish on create and delete) and a per-run Redis lock; until then document "exactly one active scheduler". **Verify:** add `tests/unit/test_scheduler.py::TestSchedulerServiceLifecycle::test_job_added_by_paused_scheduler_is_run_by_active_scheduler`; `git grep -n wakeup -- src` is empty today.

### P1 — error and load paths, missing tests, gates

- [ ] **R14 · Drain mode starts only after the server has stopped accepting connections** `[run]`. `src/downloader/main.py:215-220` (only caller of `start_drain()`); `src/downloader/drain.py:84-105`; `Dockerfile:69`. uvicorn closes its sockets before lifespan shutdown, so the `503 service_draining` response and the `/health` 503 are never served. In-flight requests still finish through uvicorn's own graceful timeout, which is why this is not `P0`. Mutants removing `start_drain()`, `wait_until_drained()` or request tracking (`main.py:217,220`, `drain.py:104`) survive. Claims affected: `README.md:28`, `AGENTS.md:110`, `docs/features/infrastructure.md:21-22`, `spec/contracts.md:159-175,930` (propose). **Next:** enter drain on SIGTERM before uvicorn closes (wrap its handler or use a `preStop` hook); add a subprocess test that signals a real server. **Verify:** add `tests/unit/test_v070_ops_drain_worker_otel.py::TestGracefulShutdownAndDrain::test_middleware_counts_in_flight_requests` and `::test_lifespan_shutdown_drains_before_closing_clients`.
- [ ] **R15 · API-key requirement is unpinned on 9 of 15 routes.** `src/downloader/routes/render.py:56`; `src/downloader/routes/batch.py:173,238,329`; `src/downloader/routes/schedules.py:133,228,285,344,404`. Replacing `Depends(get_api_key)` on any of them keeps the gate green. **Next:** one parametrised test over `app.routes`: every route except `/health` and the docs routes answers `401` without a key. **Verify:** add `tests/api/test_auth_matrix.py::test_every_route_except_health_requires_api_key`.
- [ ] **R16 · The `/metrics*` auth tests fail when run alone.** `tests/api/test_metrics.py:59-80,331-355`. A local `api_client` fixture without lifespan relies on `app.state.pdf_semaphore` left behind by earlier tests and leaves its own mocks on the global app. **Next:** use the shared lifespan fixture; run the gate once per build under `pytest-xdist` or random order; remove the five `app.dependency_overrides.clear()` calls that wipe fixture-owned overrides. **Verify:** `uv run --no-sync pytest -p no:cacheprovider "tests/api/test_metrics.py::TestMetricsAuthentication"` (4 fail today).
- [ ] **R17 · SSRF wiring is not under test.** `tests/unit/test_http_hardening.py:59-65,88-94,165-171,196-202`; `tests/unit/test_robots.py:283-287`; `tests/unit/test_browser_recycling.py:174-179`. Every `HTTPClient` test replaces `_client` with a mock transport and re-attaches the hook by hand. Mutants that drop the SSRF-safe transport (`src/downloader/http_client.py:162`), the pinning backend (`src/downloader/ssrf_transport.py:97`), the robots redirect hook (`src/downloader/crawling/robots.py:275`) or the route guard at all three context sites (`src/downloader/pdf_generator.py:252`, `src/downloader/content_converter.py:427,525`) survive. **Next:** a constructor port for the network backend on `HTTPClient` and `RobotsChecker`; delete the `_client` swaps. **Verify:** add `tests/unit/test_ssrf_wiring.py::test_http_client_blocks_rebinding_between_preflight_and_connect`, `::test_http_client_transport_is_ssrf_safe_and_ignores_proxy_env`, `::test_robots_default_client_revalidates_redirects` and `tests/unit/test_browser_recycling.py::TestPlaywrightSSRFRouteGuard::test_every_context_factory_installs_route_guard`; `git grep -n -E "SSRFSafeHTTPTransport|trust_env" -- tests` is empty today.
- [ ] **R18 · SSRF pre-flight is not pinned per ingress.** `src/downloader/routes/render.py:61`; `src/downloader/scheduler/executor.py:182`; `src/downloader/routes/download.py:112`; `src/downloader/crawling/robots.py:300`. No test sends a private address to `POST /render` (mutant survives); the run-time check for schedules is tested only in the deselected `integration` tier; the `GET /{url}` check is held by one malformed-URL test. **Next:** one test per ingress asserting `400` and zero calls on robots, HTTP client and renderer. **Verify:** add `tests/unit/test_v070_screenshot_and_actions.py::TestScreenshotAndRenderEndpoints::test_post_render_rejects_private_and_metadata_urls`, `tests/api/test_download.py::TestDownloadEndpoint::test_private_address_rejected_before_robots_and_fetch`, `tests/unit/test_robots.py::TestRobotsChecker::test_origin_validated_before_any_fetch`.
- [ ] **R19 · `X-Forwarded-For`: hop order unpinned, unparsable hops skipped** `[run]`. `src/downloader/ratelimit_middleware.py:193-198`; `tests/unit/test_v061_hardening.py:416-430`. Removing `reversed(` at `:195` survives (all three test requests share one header). Entries such as `ip:port`, `[v6]:port` or `unknown` are dropped before the right-to-left walk, so behind a proxy that writes them the caller picks its own rate-limit key. **Next:** walk the raw list, strip a port, stop at the first untrusted or unparsable entry. **Verify:** add `tests/unit/test_v061_hardening.py::TestTrackBAuthDocsRateLimitAndSSRF::test_rotating_leftmost_xff_hop_stays_in_one_bucket`, `::test_resolve_client_ip_table`, `::test_xff_unparsable_hop_is_not_skipped`.
- [ ] **R20 · Webhook signature is tested against itself.** `tests/unit/test_v070_ocr_and_webhooks.py:240-313`. Mutants that sign with an empty key (`src/downloader/services/webhooks.py:39`), follow redirects (`:112`) or stop retrying on 429 (`:137`) survive. **Next:** a known-answer digest test, a two-secrets test, one test per delivery rule. **Verify:** add `test_signature_known_answer`, `test_signature_depends_on_secret`, `test_redirect_is_not_followed`, `test_429_is_retried` in `tests/unit/test_v070_ocr_and_webhooks.py::TestWebhookDeliveryAndIntegration`.
- [ ] **R21 · Webhook URLs are logged in full** `[run]`. `src/downloader/services/webhooks.py:79,139,151,163`. Userinfo credentials and query tokens reach the log on every blocked or failed delivery; `CRITICAL.md:17` lists secret redaction for this module. Related: the signature has no timestamp, and `webhook_secret` is stored in plaintext in Redis. **Next:** log scheme, host and delivery id; propose a timestamp in the signed string. **Verify:** add `tests/unit/test_v070_ocr_and_webhooks.py::test_webhook_logs_redact_url_credentials`.
- [ ] **R22 · Split-worker reliability rests on one success-path test.** `tests/unit/test_v070_ops_drain_worker_otel.py:36-146,206-226,275-312`; `src/downloader/worker.py:57-117,225-446`; `src/downloader/worker_queue.py:91-132`. One of five outcomes of `process_worker_batch_envelope` is tested, `run_worker()` has no test, gate coverage of `worker.py` is 27 %, and `requires_redis` selects 0 tests, so `BLMOVE`, `LREM` and the Lua throttle script only run as Python re-implementations. Mutants recording failed jobs as completed (`worker.py:110`) and making the queue LIFO (`worker_queue.py:97`) survive. **Next:** the path tests below; one shared fake and one contract suite that also runs against real Redis in Cloud Build. **Verify:** add `test_worker_failure_marks_failed_fires_failed_webhook_and_acks`, `test_worker_skips_missing_and_cancelled_jobs`, `test_batch_queue_is_fifo` in `TestSplitWorkerArchitecture`, and `tests/integration/test_worker_queue_redis.py` (`requires_redis`).
- [ ] **R23 · Four error branches of `GET /{url}` are asserted nowhere.** `src/downloader/routes/download.py:330-335` (422), `:351-356` (504), `:360` (502), `:367-372` (500); `tests/api/test_download.py:118-142`. Changing the 502 default to 500 survives. **Next:** one parametrised row per branch. **Verify:** extend `tests/api/test_download.py::TestDownloadEndpoint::test_download_client_errors`; `git grep -n -E "download_error|ocr_error|== 504" -- tests` is empty today.
- [ ] **R24 · 104 hermetic tests run in no gate.** `tests/conftest.py:121-134`; `tests/integration/test_scheduler.py`, `test_batch_processing.py`, `test_browser_security.py`, `test_browser_rendering.py`, `test_html_rendering.py`; `tests/api/test_health.py:6`. They carry the `integration` marker but need no browser, Redis or network, pass offline in 1.9 s, and hold the only route tests for `/schedules`. **Next:** keep `integration` for the 15 `requires_playwright` tests and mark the rest `unit` (gate 759 → 863). **Verify:** `uv run --no-sync pytest -p no:cacheprovider -q tests/integration/test_scheduler.py tests/integration/test_batch_processing.py tests/integration/test_browser_security.py tests/integration/test_browser_rendering.py tests/integration/test_html_rendering.py tests/api/test_health.py`.
- [ ] **R25 · Verification tiers 2 and 3 have no working runner.** `justfile:93-102,116`; `cloudbuild.yaml:37-53,82-123`; `AGENTS.md:203-205`. Cloud Build starts no Redis and runs neither `-m integration` nor `-m e2e`; `just e2e` prints failures and exits 0; the 15 Chromium and 14 e2e tests run only inside the catch-all `just test`. **Next:** make `just e2e` exit non-zero on failure; add a Cloud Build step with Redis that runs the browser tests and the `requires_redis` suite; until then correct the tier-3 sentence in `AGENTS.md`. **Verify:** `git grep -n -i redis -- cloudbuild.yaml` (only the deselect expression); `git grep -n "exit 1" -- justfile`.
- [ ] **R26 · SDK suites run in no gate; the JavaScript test needs an untracked build.** `sdks/javascript/test/client.test.mjs:3`; `.gitignore:25`; `cloudbuild.yaml:37-53`. The Python, JavaScript and Go SDK tests are in neither the local gate nor Cloud Build; the JavaScript test imports `../dist/index.js`, which is gitignored and has no build script, so it fails on a clean checkout. **Next:** add `go vet`, `gofmt -s -l`, `go test -race`, `tsc --noEmit`, `node --test` and the Python SDK suite to both gates; import something a clean checkout has. **Verify:** `git check-ignore -v sdks/javascript/dist/index.js`; `git grep -n -E "go vet|node --test" -- cloudbuild.yaml justfile` is empty today.
- [ ] **R27 · Failed actions and selector waits on PDF and screenshot return 500** `[fake]`. `src/downloader/pdf_generator.py:348-358,391-395,453-463,486-490`. Both methods wrap `BrowserActionError` and selector timeouts into `PDFGeneratorError`, so the `400 browser_action_error` and `408 selector_timeout_error` branches in routes, worker and batch cannot be reached for these outputs. Screenshot failures are also reported as `pdf_generation_error` (`src/downloader/routes/download.py:381-388`). **Next:** re-raise `BrowserActionError`; convert the selector timeout to `SelectorTimeoutError`. **Verify:** add `tests/unit/test_v070_screenshot_and_actions.py::test_pdf_and_screenshot_action_errors_map_to_400_and_408`.
- [ ] **R28 · Browser pool loses capacity and never recovers** `[fake]`. `src/downloader/browser/manager.py:103-128,274-309`; `src/downloader/pdf_generator.py:190-204,256-271`; `src/downloader/content_converter.py:449-464,561-576`. A failed replacement shrinks the pool for good; a cancellation during cleanup skips the release and disables the idle reaper; a failed start leaves one Playwright driver process per retry. **Next:** relaunch in `get_browser` when below `pool_size`; shield the cleanup steps; close what was started before raising in `BrowserPool.start()`. **Verify:** add `tests/test_browser_manager.py::test_pool_regains_capacity_after_failed_replacement`, `::test_start_failure_stops_playwright_driver`, `tests/unit/test_browser_recycling.py::test_cancel_during_context_close_still_releases_browser`.
- [ ] **R29 · Force-kill path cannot run; its test mocks an attribute Playwright does not have** `[read]`. `src/downloader/browser/manager.py:139-178,205-207`; `tests/test_browser_manager.py:124`. `_get_browser_pid` reads `_impl_obj._browser_process.pid`, which the Playwright client does not have, so a hung Chromium is dropped from tracking and left running. **Next:** delete the kill path and its tests, or obtain the PID through a supported route and test against a real browser. **Verify:** `git grep -n _browser_process -- src tests`.
- [ ] **R30 · Browser semaphore admits 12 jobs for 3 browsers** `[fake]`. `src/downloader/config.py:108-110,128-133,152-157`; `src/downloader/main.py:109,130`; `src/downloader/browser/manager.py:88-90,211-231`. On 2+ vCPUs jobs 4…N pass the `503` fail-fast check, wait up to 30 s for a browser and fail as `500`. **Next:** size the semaphore from the pool, or reject the mismatch in settings validation. **Verify:** add `tests/unit/test_browser_capacity.py::test_fourth_concurrent_render_gets_503_not_500`.
- [ ] **R31 · No overall deadline on API-path browser jobs** `[fake]`. `src/downloader/content_converter.py:152-171,431-434,529-549`; `src/downloader/pdf_generator.py:360,465,492-513`; `src/downloader/browser/actions.py:127-158`. Only per-step timeouts exist. The modal closer clicks every match of four to six selectors at 1 s each with no cap, so a page with many hidden close buttons holds a browser for minutes to hours; it also closes dialogs the caller's own actions opened. **Next:** one `asyncio.timeout(budget)` per browser job; cap the closer and skip it when the caller supplied actions. **Verify:** add `tests/unit/test_render_deadline.py::test_modal_closer_is_bounded`.
- [ ] **R32 · HTML parsing and conversion block the event loop** `[run]`. `src/downloader/services/content_processor.py:258,288,308,336,585`; `src/downloader/content_converter.py:201,325,594`; `src/downloader/transformers/markdown.py:56`; `src/downloader/transformers/plaintext.py:37`. About 2 s per MiB of HTML (5 MiB: 5 s detection, 12 s markdown), during which every other request and `/health` waits; the cap allows 50 MiB and the image health check times out at 10 s. **Next:** a bounded executor for detection and conversion; parse once; a lower size limit for HTML that will be parsed. **Verify:** add `tests/unit/test_event_loop_blocking.py::test_conversion_does_not_block_loop`.
- [ ] **R33 · Non-Latin-1 URLs fail with 500 on browser outputs** `[fake]`. `src/downloader/validation.py:43-76`; `src/downloader/services/content_processor.py:255,283,479,506,534`; `src/downloader/services/content_negotiation.py:233`. `validate_url` returns its input unchanged; the raw URL goes into `X-Original-URL`, which Starlette cannot encode, after the render has completed. The same raw string keeps control characters, userinfo and a trailing dot, which feeds `R47` and `R48`. **Next:** reject control characters, whitespace and backslashes; return a canonical serialisation; add an `origin_key(url)` helper without userinfo. **Verify:** add `tests/unit/test_validation_canonical.py::test_returns_ascii_canonical_url` and `tests/api/test_download.py::test_pdf_of_non_latin1_url_returns_200`.
- [ ] **R34 · Default `Accept` returns an empty 200 for app shells** `[run]`. `src/downloader/content_converter.py:174-232` (threshold `:209-214`) against `:284-382`; `src/downloader/services/content_processor.py:116-121,338-349,585-622`. Text and markdown fall back to the browser only when the raw body already has 100 characters of text, so an app shell returns empty with no hint while the HTML path renders the same page. The threshold is pinned by `tests/unit/test_html_rendering.py:421-453`; `spec/contracts.md:455` and `:462` contradict each other and `:465` still says `networkidle`. **Next:** one pure `needs_js_render()` decision shared by all formats; propose the spec wording. **Verify:** add `tests/unit/test_html_rendering.py::test_spa_shell_text_and_html_agree_on_rendering`.
- [ ] **R35 · Comma-separated list settings stop the process at start-up** `[run]`. `src/downloader/config.py:276-279,297-303,475-493,549-568`. pydantic-settings JSON-decodes `list[str]` values before the comma-splitting validators run, so the documented format for `CORS_ALLOWED_ORIGINS`, `RATELIMIT_TRUSTED_PROXIES` and `CONTENT_HTML_JS_HEAVY_DOMAINS` (`.env.example:132,160`, `README.md:189`, `AGENTS.md:159`) raises `SettingsError`; only a JSON array works. **Next:** annotate the three fields `Annotated[list[str], NoDecode]`. **Verify:** add `tests/unit/test_config_env_lists.py::test_comma_separated_list_env_vars_parse`.
- [ ] **R36 · A `.env` file is honoured for three keys only** `[run]`. `src/downloader/config.py:382-388,633-656`. Nested settings classes read the process environment only, so `.env` sets `ENVIRONMENT`, `APP_NAME` and `DOWNLOADER_KEY` and silently ignores `REDIS_URI`, `RATELIMIT_*`, `PDF_*` and the rest. Containers are unaffected. **Next:** give every sub-settings class the same `env_file`, or load the file once in `get_settings()`. **Verify:** add `tests/unit/test_config_env_lists.py::test_dotenv_reaches_nested_settings`.
- [ ] **R37 · A blank `DOWNLOADER_KEY` turns authentication off in production with one log warning** `[run]`. `src/downloader/auth.py:28-45`; `src/downloader/config.py:663-665`. Documented in `.env.example:93`, while `README.md:145` calls the key required in production. **Next:** refuse to start in production without a key unless an explicit opt-out is set; resolve the key once from `Settings`. **Verify:** add `tests/smoke/test_auth.py::test_production_without_key_refuses_to_start`.
- [ ] **R38 · Remote browser calls have no back-pressure** `(split)` `[read]`. `src/downloader/worker_queue.py:134-150`; `src/downloader/services/content_negotiation.py:194-204`; `src/downloader/services/content_processor.py:155-157,445-446`; `src/downloader/worker.py:384-404`. Each in-flight remote render parks one connection of the 10-connection Redis pool in `BLPOP`; tasks carry no deadline, so the worker still renders them after the API has timed out. **Next:** bound remote calls per API pod and answer `503` when full; a dedicated pool for `BLPOP`; a deadline in `BrowserTaskRequest`. **Verify:** add `tests/unit/test_v070_ops_drain_worker_otel.py::TestSplitWorkerArchitecture::test_remote_browser_calls_are_bounded_and_expired_tasks_skipped`.
- [ ] **R39 · Blocking Redis calls on the event loop; start-up clears shared rate-limit counters** `(Redis)` `[read]`. `src/downloader/ratelimit_middleware.py:15-19,59-69,99-107,240-241`; `src/downloader/main.py:50-56,71`; `src/downloader/scheduler/service.py:26-40`. Synchronous `limits` storage without socket timeouts runs on every request; `reset_rate_limiter()` runs at every start and clears counters shared by all pods. **Next:** `limits.aio` storage; reset only for `memory://`; job-store calls in a thread. **Verify:** `git grep -n -E "from limits\.(storage|strategies)|reset_rate_limiter" -- src`.
- [ ] **R40 · Error responses differ from the documented envelope** `[run]`. `src/downloader/main.py:255-271`; `src/downloader/routes/download.py:358-365`; `src/downloader/routes/render.py:166-170`; `src/downloader/routes/schedules.py:203-211`; `sdks/python/src/downloader_sdk/client.py:71-91`. Request validation returns FastAPI's `422` list instead of the documented envelope (the spec says an invalid cron is `400`); upstream 5xx, 429 and 401 are passed through unchanged, so the Python SDK raises `RateLimitError` or an auth error for an upstream status; a remote render timeout is `timeout_error` where the spec says `worker_timeout`. **Next:** a validation handler that wraps errors in the envelope; map upstream 5xx and transport errors to 502; SDK branches on `error_type`; settle the spec strings in review. **Verify:** add `tests/api/test_download.py::test_upstream_5xx_maps_to_502` and `tests/api/test_error_envelope.py::test_validation_errors_use_error_envelope`.
- [ ] **R41 · Request models drop unknown fields, including `timeout_ms`** `[run]`. `src/downloader/models/responses.py:37-118`; `src/downloader/browser/actions.py:38-83`; `spec/contracts.md:374-432`. `POST /render` documents `timeout_ms` but `RenderRequest` has no such field; `{"type":"wait","ms":3000}` waits the 500 ms default; unknown batch `format` values fall through to raw and report success. **Next:** implement `timeout_ms` or propose removing it; forbid extra fields; type formats as literals; cap the batch URL length. **Verify:** add `tests/unit/test_v070_screenshot_and_actions.py::test_render_request_rejects_unknown_fields`.
- [ ] **R42 · Schedule history `total` counts expired runs** `(Redis)` `[fake]`. `src/downloader/scheduler/storage.py:70-77,117-131,144-146`. Run records expire after 24 h but their ids stay in a sorted set whose TTL is renewed on every run, so `total` overstates and old pages come back short or empty; `delete_executions` (`:148-177`) has no caller. **Next:** trim with `ZREMRANGEBYSCORE` in `store_execution`; delete history with the schedule. **Verify:** add `tests/unit/test_scheduler.py::TestExecutionStorageOperations::test_total_excludes_expired_executions`.
- [ ] **R43 · A batch's results are one unbounded Redis value** `(Redis)` `[read]`. `src/downloader/services/batch_processor.py:526,548-557`; `src/downloader/job_manager.py:320-336`. With the defaults (50 URLs × 50 MB) the single `SETEX` can pass Redis's 512 MB string limit after all the work is done. **Next:** cap total result bytes per job, or store one key per URL. **Verify:** add `tests/api/test_batch_processing.py::test_batch_result_aggregate_size_is_capped`.

### P2 — boundaries, contract drift, test coupling

- [ ] **R44 · No browser-session, renderer or egress-policy port** (Ports, Adapters and Contracts). `src/downloader/pdf_generator.py:230-271,516-604`; `src/downloader/content_converter.py:404-464,500-576`; `src/downloader/browser/manager.py:371-397`; `src/downloader/http_client.py:156,187,235-238`. The browser session (acquire, context, guard, page, cleanup) is written three times, so `R1`, `R27`, `R28`, `R31` and `R49` each need the same fix in three places; local versus remote rendering is chosen by `if worker_queue is not None` in five places; redirect validation reads global settings while routes pass `app.state.settings`. **Next:** a `BrowserSession` context manager; a `BrowserRenderer` protocol with local and remote adapters chosen once in `main.py`; an `EgressPolicy` object; `HTTPClient(transport=…)`. **Verify:** `git grep -n -E "worker_queue is not None|worker_queue is None" -- src/downloader/services` (5 hits today, target 0).
- [ ] **R45 · Service layer returns FastAPI `Response`; four parallel format pipelines drift.** `src/downloader/services/content_processor.py:12,226-359,362-429,489-638`; `src/downloader/services/multi_format.py:31-176`; `src/downloader/services/content_negotiation.py:139-174`. Batch and scheduler re-implement format handling (batch HTML never auto-renders; multi-format drops `force_ocr` and can render one URL three times); `multi_format.py` imports `content_processor` back inside functions. **Next:** one `convert(request, deps) -> ConversionResult` in the core; routes map results and typed errors to HTTP. **Verify:** `git grep -n "from fastapi import" -- src/downloader/services/content_processor.py src/downloader/services/multi_format.py src/downloader/services/content_negotiation.py` (3 hits, target 0).
- [ ] **R46 · Production code carries hooks for tests; no job-store or work-queue port.** `src/downloader/services/batch_processor.py:48-53,69-73,86-99`; `src/downloader/routes/batch.py:29-49`; `src/downloader/routes/schedules.py:30-46`; `src/downloader/http_client.py:76-94,235-244`; `src/downloader/pdf_generator.py:30-34,63,88-90`; `src/downloader/auth.py:41`; `src/downloader/config.py:393`; `src/downloader/crawling/robots.py:306,325`. `batch_processor` imports `main.app` and resolves functions from `sys.modules` so old monkeypatches still hit; `install_ssrf_route_guard` skips the guard when the context has no `route` attribute; two branches test whether pytest is loaded. Tests use 115 dotted-path patch targets and three hand-written Redis fakes. **Next:** pass collaborators as parameters and delete `_resolve_batch_hook` and the re-exports; define `JobStore` and `WorkQueue` protocols with one in-memory adapter shared by tests. **Verify:** `git grep -n -E "_resolve_batch_hook|from \.\.main import" -- src` must return nothing.
- [ ] **R47 · robots.txt: transient failures cached as allow-all for an hour; credentials in cache keys and logs** `[run]`. `src/downloader/crawling/robots.py:220-257,286,296-343,359-365`. 5xx and timeouts are cached as allow-all for the full 3600 s TTL; control flow matches exception text; the cache key is the raw netloc including userinfo; memory is bounded by entry count only (1000 × 512 KiB); redirect targets on other origins get no robots check. **Next:** a short negative TTL (about 60 s); a typed `DNSResolutionError`; key on `(scheme, host, port)`; cap stored bytes. **Verify:** add `tests/unit/test_robots.py::test_failure_sentinel_uses_short_ttl`, `::test_cache_key_excludes_userinfo`, `::test_wildcard_and_longest_match_rules`.
- [ ] **R48 · Crawl-delay throttle: unbounded queueing and key bypass** `[run]`. `src/downloader/crawling/throttle.py:61-65,82-96,136-164`. Each request reserves the next slot and sleeps with no maximum and no refund on disconnect (50 queued requests at a 30 s delay: the last waits 1470 s); batch holds its semaphores while sleeping; a trailing dot, explicit port or userinfo gives a separate bucket. **Next:** bound the wait and answer 429/503 with `Retry-After`; key on the canonical host; delete the non-atomic Redis branch (`:103-134`). **Verify:** add `tests/unit/test_throttle.py::test_wait_is_bounded_and_key_is_canonical`.
- [ ] **R49 · Browser output has no size bound; downloads allowed; `--no-sandbox` has no decision record** `[read]`. `src/downloader/content_converter.py:438,553`; `src/downloader/pdf_generator.py:245-251,364-378,474`; `src/downloader/browser/manager.py:52,387-397`. **Next:** reject rendered output above the cap; `accept_downloads=False`; record the sandbox decision in `DECISIONS.md` or enable the sandbox. **Verify:** `git grep -n -E "accept_downloads|max_download_size" -- src/downloader/browser src/downloader/pdf_generator.py src/downloader/content_converter.py` (no hits today).
- [ ] **R50 · OCR shares the default thread pool with DNS resolution; pixel cap enforced at twice the value** `[run]`. `src/downloader/transformers/ocr.py:62-91,119-125`; `src/downloader/validation.py:247`. Six OCR jobs on a 2-vCPU pod stall every DNS-backed validation; Pillow raises only above 2 × `MAX_IMAGE_PIXELS`. **Next:** a dedicated bounded executor; compare `img.size` with the cap explicitly. **Verify:** add `tests/unit/test_v070_ocr_and_webhooks.py::test_pixel_cap_enforced_at_configured_value`.
- [ ] **R51 · Caller-supplied headers are forwarded to another origin on redirect** `(Redis)` `[run]`. `src/downloader/http_client.py:240-245`. httpx strips only `Authorization` and `Cookie`; a token sent as a custom batch or schedule header goes to the redirect target. **Next:** drop caller headers when scheme, host or port changes. **Verify:** add `tests/unit/test_http_client_isolation.py::test_custom_headers_not_forwarded_cross_origin`.
- [ ] **R52 · `PDF_WAIT_UNTIL` cannot turn off the network-idle gate** `[fake]`. `src/downloader/pdf_generator.py:306,325-346`. PDF always runs a fatal `wait_for_load_state("networkidle")` after `goto`, unlike the bounded settle that `v0.6.2` gave HTML and screenshot. **Next:** reuse `_navigate_and_settle` for PDF. **Verify:** add `tests/unit/test_v062_playwright_timeout.py::test_pdf_honours_wait_until_without_networkidle_gate`.
- [ ] **R53 · Five environment variables in the spec do not exist in code; three defaults differ** `[run]`. `spec/contracts.md:1587-1596` against `src/downloader/runtime_config.py:20-31,40-45,62-72,83-88,111-116`; repeated in `README.md:154-159` and `AGENTS.md:170-173`. Spec `OCR_TIMEOUT_SECONDS` 30 is `OCR_TIMEOUT` 15 in code; `OCR_MAX_PIXELS` is `OCR_MAX_IMAGE_PIXELS`; `WEBHOOK_TIMEOUT_SECONDS` is `WEBHOOK_TIMEOUT`; `WORKER_BROWSER_TASK_TIMEOUT` 60 is `WORKER_REMOTE_BROWSER_TIMEOUT` 45; `WORKER_POLL_TIMEOUT` is 5 against 2; `SHUTDOWN_POLL_INTERVAL` has no setting. Setting the spec names has no effect. **Next:** code-side aliases for the four renamed variables; propose the defaults and `SHUTDOWN_POLL_INTERVAL` for spec review. **Verify:** add `tests/smoke/test_documentation_contract.py::test_spec_env_vars_resolve_to_settings_fields`; `git grep -n -E "OCR_TIMEOUT_SECONDS|WORKER_BROWSER_TASK_TIMEOUT" -- src` is empty today.
- [ ] **R54 · Constant-time key comparison has no guard.** `src/downloader/auth.py:79`; `tests/unit/test_v061_hardening.py:347-358`. Replacing `secrets.compare_digest` with `==` survives every test. **Next:** an AST guard with a positive control. **Verify:** add `tests/smoke/test_auth.py::TestAuthFunctions::test_verify_api_key_uses_compare_digest`; `git grep -n compare_digest -- tests` is empty today.
- [ ] **R55 · Standards ratchet cannot see the missing `BLE` rule set.** `tests/test_standards_baseline.py:88-89`; `tests/standards_debt.json:2-15`; `pyproject.toml:43-51`. `_selects()` uses `startswith`, so selecting `B` counts as selecting `BLE`; `ruff --select BLE` reports 79 `BLE001` in `src/` with no debt entry. The recorded `E722` ignore can be dropped (0 hits). **Next:** compare rule-set prefixes exactly; add `ruff:BLE` to the debt file; fix the shared copy of the baseline test too. **Verify:** `uv run --no-sync ruff check --no-cache --select BLE --statistics src/`; `git grep -n BLE -- tests/standards_debt.json` is empty today.
- [ ] **R56 · Hygiene and baseline guards have no positive control; governance check absent.** `tests/smoke/test_repo_hygiene.py:12-21,49-65,100-114`; `tests/test_standards_baseline.py:74-139`. No synthetic-violation test; no check of `AGENTS.md` size, the `CLAUDE.md` symlink or the `CRITICAL.md` and `DECISIONS.md` caps; `SKIP_DIRS` omits `node_modules`, `dist`, `build`. **Next:** add `test_hygiene_patterns_positive_control`, `test_baseline_scanner_positive_control`, `test_governance_spine_and_claude_symlink`. **Verify:** `git grep -n -i -E "positive|tmp_path" -- tests/smoke/test_repo_hygiene.py tests/test_standards_baseline.py` is empty today.
- [ ] **R57 · No coverage floor; idle tooling; dead markers.** `pyproject.toml:65-96,106-119`. No `[tool.coverage]` and no gate passes `--cov` (measured 83 % offline; `worker.py` 27 %, `routes/schedules.py` 45 %); `pip-audit`, `pytest-xdist` and `pytest-benchmark` are unused; `requires_redis` selects nothing. **Next:** `fail_under = 83` and ratchet up; `--cov=src` in both gates. **Verify:** `git grep -n -E "fail_under|--cov" -- pyproject.toml cloudbuild.yaml justfile` is empty today.

### P3 — dead code, duplication, docs

- [ ] **R58 · Silent fallbacks.** `src/downloader/content_converter.py:693` returns the raw HTML source as the text or markdown body on any conversion exception (reproduced with 500 nested elements); `src/downloader/http_client.py:321` always reports `healthy` and `src/downloader/middleware.py:229` reads a key never produced; `src/downloader/ratelimit_middleware.py:64-67,104-107` swallow the reset from `R39`, and `:179-188` trusts `X-Forwarded-For` fully for requests without a client address; `src/downloader/pdf_generator.py:388,484,510,512` and `src/downloader/http_client.py:84` are redundant. `src/downloader/telemetry.py:146,154` and `src/downloader/content_converter.py:168,170` are legitimate cleanup. **Next:** a typed, logged conversion error; delete the redundant handlers; log the legitimate ones at debug. **Verify:** add `tests/unit/test_content_converter.py::test_conversion_failure_is_not_returned_as_raw_html`.
- [ ] **R59 · Dead code, dead parameters, pass-through wrappers, dead config.** Pipeline: `src/downloader/validation.py:139-156,191-220,255-257` (sync twin with 100 test call sites and no production caller), `src/downloader/http_client.py:69-73,119-120,198-222,337-354`, `src/downloader/content_converter.py:697-705`, `src/downloader/services/content_processor.py:362-429`, unused options in `src/downloader/transformers/markdown.py:33-36,66-87` and `plaintext.py:14-17,47-68`, `src/downloader/browser/manager.py:353-369`. Surface: `src/downloader/scheduler/service.py:202-267`, `src/downloader/scheduler/storage.py:83-97`, `src/downloader/logging_config.py:257-279`, `src/downloader/metrics.py:258-268,356-358`, `src/downloader/dependencies.py:138-155,247`, unused models in `src/downloader/models/responses.py:137-147,180-204`, the vestigial slowapi handler (`src/downloader/main.py:266-271`), dead `else` arms at `src/downloader/services/batch_processor.py:209-217,247-255`. Config: `cache_cleanup_interval` and `app_version` have no reader (`src/downloader/config.py:250-255,625`); `.env.example:39,66,132,187` shows non-default values as defaults; `WORKER_HEARTBEAT_TTL` may be set below the interval. `JobManager` is not a pass-through wrapper (it owns the WATCH/MULTI update and the task registry). **Next:** delete in one pass after `R44`–`R46`. **Verify:** `git grep -n -w -E "_check_ssrf_protection|RequestPriority|get_pool_stats|get_scheduler_service|log_with_context|get_recent_metrics|RateLimiterDep" -- src`.
- [ ] **R60 · Duplicated logic.** Main-content selector and strip lists three times with drift (`src/downloader/content_converter.py:597-623`, `src/downloader/transformers/markdown.py:59-78`, `plaintext.py:40-59`), and every conversion parses twice; modal closer twice (`src/downloader/content_converter.py:152-171`, `src/downloader/pdf_generator.py:492-513`); capacity check five times; format/MIME table three times (`src/downloader/services/content_negotiation.py:24-101`); response-header block ten times in `content_processor.py`. **Next:** one definition each, as part of `R44` and `R45`. **Verify:** `git grep -n -c "X-Original-Content-Type" -- src/downloader/services/content_processor.py`.
- [ ] **R61 · Complexity hotspots are ungated.** `tests/standards_debt.json` lists `ruff:C90` and the `PLR09xx` rules as config gaps. Worst `C901`: `src/downloader/services/batch_processor.py:56` (33), `src/downloader/routes/download.py:65` (32), `src/downloader/worker.py:225` (28), `src/downloader/crawling/robots.py:75` (20), `src/downloader/services/multi_format.py:31` (17), `src/downloader/browser/actions.py:111` (16). The format dispatch and the exception-to-status table are written out four times (`routes/download.py:147-407`, `routes/render.py:84-194`, `services/batch_processor.py:105-452`, `scheduler/executor.py:271-326`); `routes/download.py:309` reports any deep `ValueError` as a client error. No file exceeds 800 non-blank lines (largest: `config.py`, 606). **Next:** one handler table and one exception table, then select `C90` and `PLR` and delete the debt entries. **Verify:** `uv run --no-sync ruff check --no-cache --select C901 src/downloader`.
- [ ] **R62 · Banner and step-narration comments.** `src/downloader/logging_config.py:131-133,156-158,207-209` and the steps through `setup_logging`; `src/downloader/validation.py:286-332`. Counted by the `narration_comments: 18` ratchet. **Next:** delete them and lower the ratchet. **Verify:** `git grep -n -E "# ={10,}" -- src`.
- [ ] **R63 · Documentation that describes behaviour the code does not have.** `README.md:51-52` (a bare `curl` sends `Accept: */*`, which returns raw passthrough, not text); `README.md:33` and `docs/guides/deployment.md:231` (`/jobs/*` is in the 200/min bucket, not 20); `README.md:15` (no `Retry-After: 5` on browser saturation); `README.md:189` (CORS default has four origins); `docs/guides/scheduling.md:32` (no in-memory scheduler fallback); `docs/guides/deployment.md:35,212,215,247` (port 80; the service listens on 8000); `docs/features/infrastructure.md:27` (Redis key names); `AGENTS.md:160` (the browser sends a hard-coded `Chrome/120` user agent unless `USER_AGENT` is set, while robots rules are evaluated for the default agent, `src/downloader/pdf_generator.py:23-58`); scheduled downloads keep only the output size (`src/downloader/scheduler/executor.py:227-248`). Spec proposals for review: `ocr_lang` pattern (`spec/contracts.md:202` against `src/downloader/transformers/ocr.py:18`), stale examples for `/health`, `/metrics/performance` and `/metrics/live` (`:133,665,733`), default-format wording (`:82` against `:335`), retry-delay wording (`:1564`). **Next:** correct the guides; send the spec items to review. **Verify:** `git grep -n -E "Retry-After: 5|localhost:80[^0-9]|downloader:queue" -- README.md docs`.
- [ ] **R64 · Metrics exposition.** `src/downloader/metrics.py:112-113,207-211` (two unrelated series under one label, so sums double), `:225-233` (histogram without `_sum` and `_count`), `:351-392` (`html_rendering_*` recorded, never exported), `:314` (health score counts every 4xx as an error; threshold 60 against the spec's 50); `src/downloader/middleware.py:208-215` (`pdf_pool_available` is a constant); `monitoring/grafana-dashboard.json:14` reads a gauge no code sets; `monitoring/prometheus.yml:15-20` scrapes without credentials. **Next:** split the counter, export the missing series, exclude upstream 4xx from the score. **Verify:** `git grep -n -E "health_score|pdf_pool_available" -- src monitoring`.
- [ ] **R65 · SDK coverage gaps.** Multi-format `Accept`, job cancel, schedule detail and history exist only in the Python sync client; Go can only create schedules although `spec/contracts.md:1051` says the SDKs cover `/schedules`; Python `download_multi` raises a bare `JSONDecodeError` when the server returns a single format (`sdks/python/src/downloader_sdk/client.py:262-264`); `get_schedule_history` drops `total` (`:402-411`); Go `BatchJobStatus` lacks `expires_at` (`types.go:91-104` in `sdks/go`). **Next:** close the gaps or narrow the spec sentence. **Verify:** `git grep -n -E "def (cancel_job|get_schedule|get_schedule_history|download_multi)" -- sdks/python/src`.
- [ ] **R66 · Worker health check and `/health` details.** `src/downloader/worker.py:200-222` passes if any worker's heartbeat exists; `/health` is unauthenticated, returns worker hostnames, queue depths and raw Redis exception text, and scans the Redis keyspace on every probe (`src/downloader/main.py:357-359,409-419`; `src/downloader/worker_queue.py:189-199`); `JobInfo.expires_at` is fixed at creation while the TTL is renewed (`src/downloader/job_manager.py:180,281`). **Next:** check this process's own heartbeat key; trim the unauthenticated payload. **Verify:** `git grep -n -E "list_active_workers|expires_at" -- src`.
- [ ] **R67 · Five legacy test files duplicate 72 cases, and the gate runs both copies.** `tests/test_auth.py`, `tests/test_http_client.py`, `tests/test_validation.py`, `tests/test_rate_limiting.py`, `tests/test_ssrf_protection.py`. **Next:** delete them; move the other root-level test files into `tests/unit/`. **Verify:** `uv run --no-sync pytest -p no:cacheprovider --collect-only -q tests/test_auth.py tests/test_http_client.py tests/test_validation.py tests/test_rate_limiting.py tests/test_ssrf_protection.py` (72 tests).
- [ ] **R68 · Fakes and builders are redefined per file.** Four stand-ins for Redis (`tests/unit/test_robots.py:16-69`, `tests/unit/test_v070_ops_drain_worker_otel.py:36-146`, `tests/test_job_manager.py:24-40`, `tests/fixtures/mock_fixtures.py:19-22`); 31 of 38 hand-built download results omit keys production always sets; 12 of 487 mocks use `spec`. **Next:** one `FakeRedis`, one `make_response_metadata()` and one mock-transport helper in `tests/fixtures/`. **Verify:** `git grep -n -E "^class _?(Fake|Mock)[A-Za-z]*|^def _attach_mock_transport" -- tests`.
- [ ] **R69 · Tests that cannot fail.** `tests/smoke/test_rate_limiting.py:14-24,50-52,76-87,96-97`; `tests/api/test_health.py:29-35,53-55,73`. `hasattr` and `isinstance` checks on typed settings, and branches that pass in both arms. **Next:** delete all but the config and format checks; make the health tests deterministic. **Verify:** `git grep -n -c -E "hasattr\(|isinstance\(" -- tests/smoke/test_rate_limiting.py`.
- [ ] **R70 · Size-cap boundary is untested.** `src/downloader/http_client.py:192,253,263`. Changing `>` to `>=` survives. **Next:** a body of exactly `max_download_size` bytes is accepted, for the stream and for `Content-Length`. **Verify:** add `tests/unit/test_http_hardening.py::TestHTTPStreamingAndRedirectSSRF::test_body_exactly_at_limit_is_accepted`.
- [ ] **R71 · Tool versions and helper scripts drift from the gate.** `.pre-commit-config.yaml:11` pins ruff `v0.13.3` while `uv.lock` pins `0.14.13`; `justfile:5-17` has no read-only check and `just test` runs all 892 tests; `scripts/test_all.sh` is a fourth gate dialect; `run_examples.sh:5,43` exits 1 after the first successful example (`set -e` with `((SUCCESSFUL++))`). **Next:** a `just check` recipe running the four gate commands; delete `scripts/test_*.sh`; fix or delete `run_examples.sh`. **Verify:** `git grep -n "SUCCESSFUL++" -- run_examples.sh`.
- [ ] **R72 · `tests/` cannot be type-checked.** `pyproject.toml:6,71`. Tests import `src.downloader.*` while the package installs as `downloader`, so `mypy src/ tests/` stops with "source file found twice"; `requires-python` still says `>=3.10` although `src/downloader/worker_queue.py:11` needs 3.11. **Next:** `explicit_package_bases = true` with `mypy_path = "."`, add `tests/` to the mypy gate, raise `requires-python`. **Verify:** `uv run --no-sync mypy --cache-dir=/dev/null src/ tests/` (errors today).
- [ ] **R73 · e2e fixture skips on infrastructure failure; test docs are stale.** `tests/e2e/conftest.py:16-24`; `tests/e2e/test_html_rendering_e2e.py:252`; `tests/README.md:124,152,174,333,350`. **Next:** fail instead of skip; replace the README command list with the single gate command. **Verify:** `git grep -n "pytest.skip" -- tests/e2e`.

### P4 — optional

- [ ] **R74 · Small ergonomics in the pipeline.** `time.time()` for durations (`src/downloader/services/content_processor.py:156,172,595`); browser recycling runs inside the request's `finally` (`src/downloader/browser/manager.py:244-253`); full URLs with userinfo and query tokens are logged (`src/downloader/http_client.py:230`, `src/downloader/crawling/robots.py:333`); robots text is re-parsed on every request (`robots.py:383-395`). **Next:** fix when the files are next touched. **Verify:** `git grep -n "time.time()" -- src/downloader/services/content_processor.py`.
- [ ] **R75 · Tooling ideas.** A documentation-contract test that maps every environment variable in the spec, `README.md`, `AGENTS.md` and `.env.example` to a settings field and default (would have caught `R35` and `R53`); SDK wire tests driven against the FastAPI app in-process (would have caught `R2` and `R9`); batch, queue and scheduler tests on one shared in-memory adapter. **Next:** take the first with `R53`. **Verify:** `git grep -n test_documentation_contract -- tests` (no hits today).
- [ ] **R76 · conftest path handling and warnings.** `tests/conftest.py:125` uses absolute path parts, so a checkout under a directory named `integration` or `e2e` deselects everything; the offline guard (`:86-118`) does not cover asyncio streams and has no test; `pyproject.toml:92-94` ignores the Starlette deprecation warning about `httpx` in the test client; three tests use real sleeps. **Next:** `item.path.relative_to(config.rootpath).parts`; add `test_offline_guard_blocks_public_connect`. **Verify:** `git grep -n "item.path.parts" -- tests/conftest.py`.

---

## Priority Areas

### ✅ P0: Production Readiness (Phase 4 — Complete)

**Completed:** 2026-01-18  
**Archive:** [docs/features/production-readiness.md](features/production-readiness.md)

CI/CD pipeline, offline smoke/unit/hygiene verification gate (`tests/smoke/test_repo_hygiene.py` + deterministic fixture DNS), container hardening, structured logging, and documentation alignment.

---

### ✅ P1: Ethical Crawling & Stateless Safety (Phase 7 — Complete)

**Completed:** 2026-11-01  
**Archive:** [docs/features/ethical-crawling.md](features/ethical-crawling.md)

**Goal**: Prevent egress abuse and IP bans via responsible `robots.txt` compliance and `Crawl-delay` pacing that work out-of-the-box in stateless single-key deployments as well as Redis-backed clusters.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Configurable `USER_AGENT` & `RESPECT_ROBOTS_TXT` | P1 | Low | ✅ Complete (`S4-be-1`) |
| `robots.txt` parser + bounded in-memory TTL/LRU cache (optional Redis) | P1 | Medium | ✅ Complete (`S4-be-2`) |
| Per-domain `Crawl-delay` throttling (in-memory + optional Redis) | P1 | Medium | ✅ Complete (`S4-be-3`) |
| `GET /{url}` & `POST /batch` pre-flight enforcement | P1 | Medium | ✅ Complete (`S4-be-4`) |
| Structured `HTTP 403` disallow responses & metrics | P1 | Low | ✅ Complete (`S4-be-5`) |

#### Delivered Configuration & Wire Behavior
- Settings in `src/downloader/config.py` (`CrawlingConfig`): `RESPECT_ROBOTS_TXT` (`default: True`), `USER_AGENT` (`default: REST-API-Downloader/1.0`), `ROBOTS_CACHE_TTL` (`default: 3600`), `ROBOTS_CACHE_MAX_SIZE` (`default: 1000`), `ROBOTS_FETCH_TIMEOUT` (`default: 5.0`), `MAX_CRAWL_DELAY` (`default: 30.0`)
- Cache backend chain: bounded in-memory TTL/LRU cache (TTL 1h) when `REDIS_URI` is unset; Redis shared cache (`robots:v1:{netloc}`) when `REDIS_URI` is configured
- Fail-open with structured warning log + cached sentinel when `robots.txt` is unreachable; returns `HTTP 403` (`error_type: "robots_disallowed"`) when blocked

#### Success Criteria Met
- ✅ `robots.txt` and `Crawl-delay` enforced before single and batch downloads without requiring Redis
- ✅ Caching eliminates repeat `robots.txt` fetches within TTL across memory and Redis backends
- ✅ 100% offline-sandbox-safe unit and mocked API test coverage (`tests/unit/test_robots.py`, `tests/api/test_ethical_crawling.py`)

**Core Archive:** [docs/features/downloader-core.md](features/downloader-core.md)

---

### ✅ P1: Content Transformation (Phase 5 — Complete)

**Goal**: Transform downloaded content into formats optimized for downstream consumption (LLMs, data pipelines).

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| HTML to Markdown | P1 | Medium | ✅ Complete |
| Plain text extraction | P2 | Low | ✅ Complete |
| `Accept` content negotiation (single raw vs multi-format JSON) | P1 | Medium | ✅ Complete |
| Image to Text (OCR) | P2 | High | ✅ Complete (`v0.7.0`) |

#### API Implemented
- `Accept` header negotiation on `GET /{url}`: raw payload for single MIME type (`text/plain`, `text/markdown`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, `application/json`) and JSON map for multi-format `Accept` headers
- OCR image-to-text (`?ocr=true`, `?ocr_lang=eng`, automatic extraction on image origins for `text/plain` / `text/markdown` / `text/html`)
- Batch support: `format` (`text`, `markdown`, `html`, `pdf`, `screenshot`, `json`, `raw`) and `default_format` options in request body

**Archive:** [docs/features/content-transformation.md](features/content-transformation.md)

---

### ✅ P2: Browser Rendering (Phase 6 — Complete)

**Goal**: Capture JavaScript-rendered content, screenshots, and PDFs from SPAs and dynamic websites without penalizing non-browser startup or mocked test latency.

#### Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Basic JS rendering (`?render=true`) | P1 | High | ✅ Complete |
| Wait for CSS selectors (`?wait_for=`) | P1 | Medium | ✅ Complete |
| Lazy shared `BrowserPool` & process isolation | P1 | Medium | ✅ Complete |
| Security hardening (`file:` scheme blocked, isolated contexts) | P1 | Medium | ✅ Complete |
| Single-pass `domcontentloaded` + bounded settle (`S6-br-1`) | P1 | Low | ✅ Complete (`v0.6.2`) |
| Screenshot/PDF output toggle (`?output=screenshot\|pdf`) | P2 | Medium | ✅ Complete (`v0.7.0`) |
| Interaction scripting (`POST /render`, `?actions=`) | P3 | High | ✅ Complete (`v0.7.0`) |

**Archive:** [docs/features/browser-rendering.md](features/browser-rendering.md)

---

### 🚧 P3: Scheduling & Multi-Tenant Quotas (Phase 8 — Scheduling & Webhooks Complete, Quotas Deferred)

**Goal**: Support recurring downloads with webhook notifications (when Redis is deployed) and defer per-tenant quota accounting until multi-key or Redis-backed tenancy is required.

#### Scheduling Features (Sprint 3 + `v0.7.0` — Complete)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| Cron-based scheduling | P1 | Medium | ✅ Complete |
| Schedule CRUD API (`/schedules`) | P1 | Low | ✅ Complete |
| Paginated execution history (`/schedules/{id}/history`) | P2 | Low | ✅ Complete |
| Webhook on completion | P2 | Medium | ✅ Complete (`v0.7.0`) |

#### Multi-Tenant Quota Features (Post-`v0.7.0` / `v0.8.0+` — Deferred Preview)

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| `QuotaConfig` & per-API-key counters (`memory://` + Redis parity) | P2 | Medium | 📋 Post-`v0.7.0` (Deferred) |
| Enforcement middleware (`X-RateLimit-{Limit,Remaining,Reset}`) | P2 | Medium | 📋 Post-`v0.7.0` (Deferred) |
| Usage tracking endpoint (`GET /usage`) | P2 | Low | 📋 Post-`v0.7.0` (Deferred) |
| `HTTP 429` with `Retry-After` header | P2 | Low | 📋 Post-`v0.7.0` (Deferred) |
| Tier-based plans | P3 | Medium | 📋 Post-`v0.7.0` (Deferred) |

**Scheduling Archive:** [docs/features/scheduling.md](features/scheduling.md)

---

## Release `v0.7.0` Scope (Extended Features & Operations — Complete)

### Extended Features (`v0.7.0`)

| Feature | Description | Complexity | Priority | Status |
|---------|-------------|------------|----------|--------|
| Screenshot/PDF output | `?output=screenshot\|pdf`, `?full_page=`, `?screenshot_format=`, `Accept: image/png\|jpeg` | Medium | P2 | ✅ Complete (`v0.7.0`) |
| Webhook notifications | SSRF-safe HMAC-SHA256 webhooks on batch/schedule completion | Medium | P2 | ✅ Complete (`v0.7.0`) |
| OCR (Image to Text) | Pytesseract + Pillow image-to-text extraction with decompression-bomb guard | High | P2 | ✅ Complete (`v0.7.0`) |
| SDK/Client Libraries | Typed Python, JavaScript/TypeScript, and Go clients (`sdks/`) | Medium | P3 | ✅ Complete (`v0.7.0`) |
| Interaction scripting | Declarative allowlisted Playwright actions (`?actions=`, `POST /render`, `/batch`) | High | P3 | ✅ Complete (`v0.7.0`) |

### Operations & Infrastructure (`v0.7.0`)

| Feature | Description | Complexity | Priority | Status |
|---------|-------------|------------|----------|--------|
| Prometheus & health-score metrics | `/metrics`, `/metrics/performance`, `/metrics/health-score`, `/metrics/live` | Low | P1 | ✅ Complete |
| Graceful shutdown with connection drain | Zero-downtime pod termination draining active downloads (`drain.py`) | Low | P2 | ✅ Complete (`v0.7.0`) |
| Split Worker Architecture | `DEPLOYMENT_MODE=all\|api\|worker` with Redis `BLMOVE` queue & browser RPC | High | P2 | ✅ Complete (`v0.7.0`) |
| OpenTelemetry integration | Opt-in distributed tracing (`OTEL_ENABLED`) across API, HTTP, Redis, Playwright, OCR, webhooks | Medium | P2 | ✅ Complete (`v0.7.0`) |

**Infrastructure Archive:** [docs/features/infrastructure.md](features/infrastructure.md)

---

## Technical Dependencies

### Content Transformation (Complete — `v0.7.0`)
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0
pytesseract>=0.3.13
Pillow>=11.0.0
# System: tesseract-ocr, tesseract-ocr-eng
```

### Browser Rendering (Complete — Lazy Shared Pool)
```
playwright>=1.40.0
# System: playwright install chromium
```

### Scheduling & Split Worker (Complete — Requires `REDIS_URI`)
```
apscheduler>=3.10.0
redis>=5.0.0
```

### Observability (Opt-In — `OTEL_ENABLED=true`)
```
opentelemetry-api>=1.27.0
opentelemetry-sdk>=1.27.0
opentelemetry-exporter-otlp-proto-http>=1.27.0
opentelemetry-instrumentation-fastapi>=0.48b0
opentelemetry-instrumentation-httpx>=0.48b0
opentelemetry-instrumentation-redis>=0.48b0
```

### Sprint 4: Ethical Crawling (Stateless-First)
```
# Python stdlib urllib.robotparser + bounded in-memory TTL/LRU cache (zero new mandatory deps)
# Optional shared cache/coordination via existing redis>=5.0.0 when REDIS_URI is set
```

---

## Risk Matrix

| Risk | Impact | Likelihood | Mitigation |
|------|--------|------------|------------|
| Browser rendering OOM or cold-start overhead | High | Low | Lazy `BrowserPool` init, isolated contexts, memory/concurrency semaphores ✅ |
| `robots.txt` origin unreachable or slow | Medium | Medium | Short fetch timeout + fail-open with structured warning log + 1h TTL cache |
| Stateless multi-pod cache divergence without Redis | Low | Medium | Per-pod bounded TTL/LRU cache is safe for `robots.txt`; optional `REDIS_URI` unifies across pods |
| Scheduler job failures | Medium | Low | Retry policies + clear `503` when `REDIS_URI` is unset ✅ |
| Quota bypass in future multi-tenant mode | Medium | Low | Server-side middleware enforcement when post-`v0.7.0` multi-tenant quotas activate |

---

## Decision Log

Canonical append-only rationale lives in [DECISIONS.md](../DECISIONS.md). Summary of roadmap milestones:

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-11-12 | Implement `v0.6.2` (`S6-br-1` single-pass Playwright settle) and full `v0.7.0` scope (all 5 Extended Features + all 3 Operations tracks) | Eliminated `10s–20s` tracker-heavy navigation timeouts and shipped screenshot/PDF toggles, allowlisted Playwright actions (`POST /render`), SSRF-safe HMAC webhooks, Tesseract OCR, typed Python/JS/Go SDKs, graceful connection drain, split API+worker Redis architecture, and opt-in OpenTelemetry tracing while preserving stateless single-pod defaults |
| 2026-11-12 | Replan `v0.7.0` around Extended Features & Operations; move Multi-Tenant Quotas past `v0.7.0` (`v0.8.0+`) | Single-key GKE production gets higher immediate value from extraction features (screenshots, webhooks, OCR, SDKs, interaction scripting) and operational resilience (graceful drain, split workers, OTel) than per-tenant quotas |
| 2026-11-02 | Release `v0.6.1` (DNS-pinned SSRF transport, single-fetch Playwright pipeline, `/metrics*` auth & prod OpenAPI disablement, and `/batch` + `/schedules` hardening) | Closed DNS-rebinding TOCTOU, XFF spoofing, unauthenticated `/metrics*` exposure, duplicate HTTP+Chromium fetches, and stateful `/batch` + `/schedules` configuration/crawling gaps while preserving stateless GKE defaults |
| 2026-11-01 | Release `v0.6.0` (Sprint 4 Ethical Crawling + production hardening) & consolidate to Roadmap-first layout | Delivered stateless-first `robots.txt`/`Crawl-delay`, streaming `413`, redirect & Playwright SSRF guards, active `429` rate limits, browser recycling/scale-to-zero, `:8000` unprivileged port, and archived Sprint 4 into `docs/features/ethical-crawling.md` |
| 2026-11-01 | Promote Ethical Crawling (stateless-first) to Sprint 4; defer Multi-Tenant Quotas to Sprint 5 | Production runs statelessly behind a single API key without Redis; ethical crawling protects live egress immediately via in-memory TTL cache + optional Redis |
| 2026-11-01 | Lazy `PlaywrightPDFGenerator` pool + offline verification gate | Eliminates Chromium startup cost on non-browser paths and guarantees deterministic offline smoke/unit hygiene checks |
| 2026-08-20 | Adopt structured `spec/` + `DECISIONS.md` + `docs/features/` archive layout | Separate status-free wire contracts (`spec/`) from roadmap milestones (`docs/roadmap.md`) and decision rationale (`DECISIONS.md`) |
| 2026-01-21 | Sprint 3 (Scheduling API) complete | Cron scheduling, retries, and execution history delivered |
| 2026-01-19 | Sprint 2 (Browser Rendering) complete | Playwright rendering, selector waits, and security isolation delivered |
| 2026-01-18 | Sprint 1 (Content Transformation) & Sprint 0 (Production Readiness) complete | Markdown/plain-text extraction, `Accept` negotiation, and SSRF hardening delivered |

---

## Release Plan

### Release 0.3.0 — Content Transformation (Complete)
- ✅ HTML to Markdown conversion, plain text extraction, and `Accept` content negotiation

### Release 0.4.0 — Browser Rendering (Complete)
- ✅ Playwright JS rendering (`?render=true`, `?wait_for=`), process isolation, and lazy shared `BrowserPool`

### Release 0.5.0 — Scheduling & Stateless Hardening (Complete)
- ✅ Cron-based scheduling CRUD & history API (`REDIS_URI` mode), explicit `503` stateless guards, and offline repo hygiene gate

### Release 0.6.0 — Ethical Crawling & Production Hardening (Complete)
- ✅ Stateless-first `robots.txt` compliance (`urllib.robotparser`, bounded in-memory TTL/LRU cache default + optional Redis)
- ✅ Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`)
- ✅ Per-domain `Crawl-delay` throttling, structured `HTTP 403` (`robots_disallowed`) responses, and Prometheus `robots_*` metrics
- ✅ Streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After`), browser max-use recycling & idle scale-to-zero, JSON `uvicorn.access` `path` fields, and unprivileged container port `:8000`

### Release 0.6.1 — Security, Single-Fetch Browser & Stateful Pipeline Hardening (Complete)
- ✅ DNS-pinned `SSRFSafeHTTPTransport` (`trust_env=False`), async DNS resolution, and hex/octal/int/mapped-IPv6/`*.localhost` SSRF blocks
- ✅ Single-fetch Playwright pipeline across `?render=true`, `?wait_for=`, and `Accept: application/pdf` (`GET /{url}`, `/batch`, `/schedules`), unified `pdf_semaphore` concurrency, and live `ContentConfig` / TTL cache wiring
- ✅ Constant-time API key verification, `/metrics*` auth enforcement, `ENVIRONMENT=production` `404` on `/docs`/`/redoc`/`/openapi.json`, right-to-left `RATELIMIT_TRUSTED_PROXIES` `X-Forwarded-For` parsing, and auth/unauth rate-limit isolation
- ✅ Creation-time SSRF & custom header validation on `/schedules` and `/batch`, `ScheduledJobExecutor` `robots.txt` + `Crawl-delay` + per-attempt `max_workers` semaphore, atomic Redis Lua `DomainThrottle`, and `robots:v2:{scheme}:{netloc}` cache keys

### Release 0.6.2 — Playwright Navigation Timeout Resilience (Complete — `F-2026-11-05-002`)
- ✅ Single-pass `wait_until="domcontentloaded"` + bounded non-fatal `networkidle` settle (`min(timeout_ms, 2500)`) across `convert_content_with_playwright_fallback` and `render_html_with_playwright` in `src/downloader/content_converter.py`, eliminating `10s–20s` timeouts on Substack/ad-heavy pages (`materializedview.io`, `novinky.cz`)

### Release 0.7.0 — Extended Features & Operations (Complete)
- ✅ **Extended Features:** Screenshot/PDF output toggle (`?output=screenshot|pdf`, `?full_page=`, `?screenshot_format=`, `Accept: image/png|jpeg`), SSRF-safe HMAC-SHA256 webhook notifications on batch/schedule completion, OCR (image-to-text via `pytesseract` + `Pillow` + `tesseract-ocr`), typed client SDKs (`sdks/python`, `sdks/javascript`, `sdks/go`), and declarative allowlisted browser interaction scripting (`?actions=`, `POST /render`, `POST /batch`)
- ✅ **Operations & Infrastructure:** Graceful shutdown with active-download connection drain (`DrainCoordinator` + `DrainMiddleware`), split worker architecture (`DEPLOYMENT_MODE=all|api|worker`, Redis `BLMOVE` queue + remote browser RPC + `docker-compose.split.yml`), and opt-in OpenTelemetry distributed tracing (`OTEL_ENABLED=false` default, OTLP HTTP/console exporters, log correlation, W3C `traceparent` propagation)

### Release 0.7.1 — Reliability and Review Findings (Next)
- Three reliability items under Deferred Preview: binary `Content-Type` guard, auto-detected render timeouts with duplicate `ERROR` logs, and the upstream `4xx` log level
- `R1`–`R76` from the 2026-11-18 review (13 `P0`, 30 `P1`, 14 `P2`, 16 `P3`, 3 `P4`), listed under Milestone `v0.7.1` — Review Findings

### Release 0.8.0+ — Multi-Tenant Quotas (Deferred — Post-`0.7.0`)
- Per-API-key `QuotaConfig`, `GET /usage`, `X-RateLimit-*` headers, `HTTP 429` `Retry-After`, and tier-based plans (`QUOTA_STORAGE_URI → REDIS_URI → memory://` parity when multi-key tenancy is needed)
- Deferred past `v0.7.0` until multi-key or Redis-backed multi-tenant deployment is required

# `downloader` — Roadmap Archive

Implemented milestones, completed sprint baselines, and historical phase matrices moved from [`../roadmap.md`](../roadmap.md). Architectural rationale lives in [`../../DECISIONS.md`](../../DECISIONS.md) and public wire contracts live in [`../../spec/contracts.md`](../../spec/contracts.md).

## v0.7.0 (Extended Features and Operations)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18 (shipped 2026-11-12).*

- **Extended Features (5/5 Complete):**
  - Screenshot/PDF output toggle (`?output=screenshot|pdf`, `?full_page=bool`, `?screenshot_format=png|jpeg`, `Accept: image/png` / `image/jpeg`, `POST /render`, and `/batch` + `/schedules` `"screenshot"` format)
  - SSRF-safe HMAC-SHA256 webhook notifications (`src/downloader/services/webhooks.py`) firing post-persistence on batch completion/failure and schedule execution (`X-Downloader-Signature-256`, `trust_env=False`, `follow_redirects=False`, creation-time SSRF validation, secret redaction)
  - Image-to-text OCR (`src/downloader/transformers/ocr.py` via `pytesseract` + `Pillow` + `tesseract-ocr`, decompression-bomb pixel cap, language validation, `HTTP 503` `ocr_unavailable` graceful degradation)
  - Typed client SDKs for Python (`sdks/python`), JavaScript/TypeScript (`sdks/javascript`), and Go (`sdks/go`) with automated test suites
  - Declarative allowlisted browser interaction scripting (`src/downloader/browser/actions.py`: 8 action types, max 20 steps, zero raw JS evaluation) across `GET /{url}?actions=`, `POST /render`, and `POST /batch`
- **Operations & Infrastructure (3/3 Complete — see [`../features/infrastructure.md`](../features/infrastructure.md)):**
  - Graceful shutdown with active-request and background-job connection drain (`src/downloader/drain.py`: `DrainCoordinator`, `DrainMiddleware`, `HTTP 503 service_draining` + `Connection: close` + `Retry-After: 5`, `/health` draining state, uvicorn `--timeout-graceful-shutdown 35`)
  - Split Worker Architecture (`DEPLOYMENT_MODE=all|api|worker`, `src/downloader/worker_queue.py` reliable Redis `BLMOVE` queue + browser RPC + heartbeats, `src/downloader/worker.py` with `--healthcheck`, `docker-compose.split.yml`)
  - Opt-in OpenTelemetry distributed tracing (`src/downloader/telemetry.py`, `OTEL_ENABLED=false` default, OTLP HTTP/console exporters, FastAPI/httpx/Redis/Playwright/OCR/webhook/worker spans, `trace_id`/`span_id` log correlation, `X-Trace-ID` response header, W3C `traceparent` propagation)

## v0.6.2 (Playwright Navigation Timeout Resilience F-2026-11-05-002)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18 (shipped 2026-11-12).*

- **S6-br-1 (P1 — Single-Pass `domcontentloaded` + Bounded Network Settle in `src/downloader/content_converter.py`):** Replaced the two-pass `networkidle` retry pattern in `convert_content_with_playwright_fallback` and `render_html_with_playwright` with a single `page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)` followed by a bounded non-fatal settle (`page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 2500))`), eliminating `10s–20s` navigation timeouts on ad- and tracker-heavy origins (`F-2026-11-05-002`), verified by `tests/unit/test_v062_playwright_timeout.py`.

## v0.6.1 (Security, Single-Fetch Browser and Stateful Pipeline Hardening)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18 (shipped 2026-11-02).*

- **Track A (Browser & Single-Fetch Pipeline):** Eliminated redundant pre-flight HTTP fetches on `Accept: application/pdf` and `?render=true` / `?wait_for=` across `text/html`, `text/plain`, `text/markdown`, and multi-format requests; unified all Chromium pathways under `pdf_semaphore` (`503` fail-fast on explicit `?render=true`/PDF, graceful static degradation on auto-detect/empty-body fallback); removed the unconditional `2s` sleep in `generate_pdf`; wired `ContentConfig` flags and monotonic-TTL `BoundedCache`.
- **Track B (Auth, SSRF Pinning & Rate Limits):** Restored `.env` loading in `AuthConfig` with `secrets.compare_digest` verification; required API key auth on `/metrics*` when `DOWNLOADER_KEY` is set and disabled `/docs`, `/redoc`, and `/openapi.json` (`HTTP 404`) in `ENVIRONMENT=production`; implemented `SSRFSafeNetworkBackend` / `SSRFSafeHTTPTransport` (`trust_env=False`, DNS-pinned `connect_tcp` preventing DNS-rebinding TOCTOU, non-blocking `asyncio.to_thread` DNS resolution, and hex/octal/int/mapped-IPv6/`*.localhost` blocks); added right-to-left `RATELIMIT_TRUSTED_PROXIES` `X-Forwarded-For` client IP resolution and auth vs unauth bucket isolation.
- **Track C & D (Stateful `/batch`, `/schedules` & Redis Atomicity):** Added creation-time SSRF & custom header sanitization (`HTTP 400`) on `POST /schedules` and `POST /batch`, `robots.txt` + `DomainThrottle` + non-retryable policy short-circuit + per-attempt `SCHEDULER_MAX_WORKERS` semaphore + JSON `content_size` accounting in `ScheduledJobExecutor`, atomic Lua `DomainThrottle` Redis slot reservation, `(scheme, netloc)` `robots:v2:{scheme}:{netloc}` cache isolation, and incremental `/batch` progress updates.

## v0.6.0 (Sprint 4 Ethical Crawling and Production Hardening)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18 (shipped 2026-11-01 — see [`../features/ethical-crawling.md`](../features/ethical-crawling.md)).*

- 7 tasks completed (`S4-be-1`..`S4-be-5`, `S4-test-1`, `S4-doc-1`).
- Configurable `USER_AGENT` and `RESPECT_ROBOTS_TXT` (`default: True`) across `HTTPClient` and Playwright.
- Stateless-first `robots.txt` evaluation (`urllib.robotparser`) with bounded in-memory TTL/LRU cache by default (TTL 1h, zero Redis dependency), optional Redis backend when `REDIS_URI` is configured, and cached fail-open sentinels on unreachable origins.
- Per-domain `Crawl-delay` throttling (in-memory timestamps by default + optional Redis coordination), `HTTP 403` `robots_disallowed` responses, and Prometheus `robots_*` counters.
- Production hardening: streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413` `content_too_large`), redirect & Playwright SSRF route blocking, active `RateLimitMiddleware` (`HTTP 429` + `Retry-After` + `X-RateLimit-*`), `BrowserPool` max-use recycling (`PDF_MAX_USES_PER_BROWSER=50`) & idle scale-to-zero (`PDF_IDLE_TIMEOUT=600s`), structured JSON `uvicorn.access` `path`/`method`/`status_code` fields, and unprivileged container port `:8000`.

## v0.3.0 to v0.5.0 (Content Transformation, Browser Rendering and Scheduling)

*Archived from [`../roadmap.md`](../roadmap.md) on 2026-11-18 (shipped 2026-01-18 through 2026-01-21).*

- **Release `0.5.0` — Scheduling & Stateless Hardening (Sprint 3, 2026-01-21):** Cron-based scheduling CRUD & paginated history API (`REDIS_URI` mode), job execution with retry, explicit `503` stateless guards, and offline repo hygiene gate — [`../features/scheduling.md`](../features/scheduling.md).
- **Release `0.4.0` — Browser Rendering (Sprint 2, 2026-01-19):** Playwright JS rendering (`?render=true`, `?wait_for=`), process isolation, `file:` scheme blocking, and lazy shared `BrowserPool` — [`../features/browser-rendering.md`](../features/browser-rendering.md).
- **Release `0.3.0` — Content Transformation & Production Readiness (Sprints 0–1, 2026-01-18):** HTML-to-Markdown conversion, plain text extraction, single- vs. multi-format `Accept` content negotiation, core downloader/batch foundation, CI/CD, container hardening, and structured logging — [`../features/content-transformation.md`](../features/content-transformation.md), [`../features/production-readiness.md`](../features/production-readiness.md), [`../features/downloader-core.md`](../features/downloader-core.md).

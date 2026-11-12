# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

---

## 2026-11-12 — `v0.6.2` single-pass browser settle & `v0.7.0` extended features and operations

- **Decision (`v0.6.2` single-pass `domcontentloaded` + bounded settle — `F-2026-11-05-002`):**
  Replaced the two-pass `networkidle` navigation draft in `src/downloader/content_converter.py`
  (`convert_content_with_playwright_fallback` and `render_html_with_playwright`) with a single
  `page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)` followed by a bounded
  non-fatal `page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 2500))`.
  - **Why:** Ad- and analytics-heavy pages hold background connections open indefinitely, causing
    two-pass retries to burn `10s–20s` and time out unnecessarily once DOM hydration has finished.
- **Decision (Screenshot/PDF output toggle & declarative allowlisted browser actions — zero raw JS eval):**
  Added `output=screenshot|pdf`, `full_page`, `screenshot_format=png|jpeg`, and `Accept: image/png` /
  `image/jpeg` across `GET /{url}`, `POST /render`, `POST /batch`, and `/schedules`. Defined
  strictly allowlisted declarative actions (`src/downloader/browser/actions.py`: `click`, `fill`,
  `select`, `scroll`, `wait`, `wait_for_selector`, `hover`, `press`; max `20` steps, bounded per-step
  timeout, zero arbitrary JavaScript evaluation).
  - **Why:** Allows multi-step SPA interaction (dismissing modals, expanding tabs, scrolling lazy
    lists) prior to extraction/capture without exposing an RCE or sandbox-escape surface.
- **Decision (SSRF-safe HMAC-SHA256 webhooks firing strictly post-persistence):**
  Added `src/downloader/services/webhooks.py` (`deliver_webhook`) using `validate_url_async` +
  DNS-pinned `SSRFSafeHTTPTransport(trust_env=False)` with `follow_redirects=False`, bounded
  retries (`WEBHOOK_MAX_RETRIES=3`), and `X-Downloader-Signature-256: sha256=<hex>` signatures.
  `webhook_url` is SSRF-validated at batch/schedule creation time, `webhook_secret` is never returned
  in API responses, and batch webhooks fire strictly after `store_job_results` + `COMPLETED` status
  updates persist in Redis.
  - **Why:** Prevents webhook endpoints from becoming a secondary SSRF pivot and eliminates race
    conditions when receivers immediately fetch `GET /jobs/{job_id}/results`.
- **Decision (Tesseract OCR with decompression-bomb protection & `503` graceful degradation):**
  Added `src/downloader/transformers/ocr.py` (`pytesseract` + `Pillow` + `tesseract-ocr` /
  `tesseract-ocr-eng` in `Dockerfile`) with `Image.MAX_IMAGE_PIXELS` capping (`OCR_MAX_PIXELS`),
  regex language validation (`OCR_DEFAULT_LANGUAGE`), and explicit `HTTP 503` (`ocr_unavailable`)
  when the `tesseract` binary is absent or `OCR_ENABLED=false`.
- **Decision (Graceful shutdown drain, split API+worker Redis architecture & opt-in OpenTelemetry):**
  Added `src/downloader/drain.py` (`DrainCoordinator`, `DrainMiddleware` returning `503` +
  `Connection: close` + `Retry-After: 5`, `/health` `503` while draining, `JobManager.wait_for_active_jobs`),
  split `DEPLOYMENT_MODE=all|api|worker` (`src/downloader/worker_queue.py` reliable `BLMOVE` batch
  queue + remote browser RPC + worker heartbeats, `src/downloader/worker.py`, `docker-compose.split.yml`),
  and opt-in OpenTelemetry (`src/downloader/telemetry.py`, `OTEL_ENABLED=false` default, OTLP HTTP /
  console exporters, `trace_id`/`span_id` log correlation, `X-Trace-ID`, W3C `traceparent` across Redis).
  - **Why:** Preserves zero-overhead stateless single-pod defaults (`DEPLOYMENT_MODE=all`,
    `OTEL_ENABLED=false`) while enabling zero-downtime Kubernetes rollouts, independent horizontal
    scaling of Playwright/scheduler workers, and end-to-end tracing when opted in.
- **Decision (Typed first-party client SDKs in `sdks/`):**
  Added `sdks/python`, `sdks/javascript`, and `sdks/go` with zero internal-tooling dependencies,
  full coverage of download, screenshot/PDF, actions (`POST /render`), OCR, batch + webhooks, and
  schedules, verified via standalone offline test suites.

---

## 2026-11-12 — Replan `v0.7.0` around Extended Features & Operations; move Multi-Tenant Quotas past `v0.7.0`

- **Decision (Roadmap reprioritization — supersedes `2026-11-01` post-Sprint-4 sequencing):**
  Pulled the former Sprint 6 Extended Features (screenshot/PDF toggle, batch/schedule
  webhooks, OCR, typed client SDKs, browser interaction scripting) and Operations &
  Infrastructure items (graceful shutdown connection drain, split worker architecture,
  OpenTelemetry tracing) into **`v0.7.0`**, and pushed **Multi-Tenant Per-Key Quotas**
  (`QuotaConfig`, `GET /usage`, per-key counters, tier-based plans) out to **post-`v0.7.0`
  (`v0.8.0+`, Deferred)**. `v0.6.2` remains the immediate next patch release for Playwright
  navigation timeout resilience (`F-2026-11-05-002`).
- **Why:** Production continues to run single-key on `services-gke`, so user-facing
  extraction features (screenshots, webhooks, OCR) and operational resilience (graceful
  drain, worker isolation, tracing) deliver immediate value ahead of multi-tenant quota
  accounting. Details in `docs/roadmap.md`.

---

## 2026-11-02 — `v0.6.1` DNS-pinned SSRF transport, single-fetch browser pipeline & endpoint hardening

- **Decision (DNS-pinned `SSRFSafeHTTPTransport` & async SSRF validation):**
  Added `src/downloader/ssrf_transport.py` wrapping `httpcore.AsyncNetworkBackend.connect_tcp`
  to resolve DNS via `asyncio.to_thread(socket.getaddrinfo, ...)`, validate all A/AAAA
  records (including IPv4-mapped IPv6, hex/octal/integer/shorthand IPv4, malformed numeric
  TLDs, and `*.localhost`), and connect directly to the validated IP while preserving the
  original hostname for TLS SNI (`trust_env=False` on both `HTTPClient` and `RobotsChecker`).
  - **Why:** Pre-flight `validate_url()` followed by a second DNS lookup inside `httpx`
    left a DNS-rebinding TOCTOU window and blocked the asyncio event loop on synchronous
    `getaddrinfo` calls.
- **Decision (Single-fetch Playwright & PDF pipeline under unified `pdf_semaphore`):**
  Skipped pre-flight `http_client.download()` on `Accept: application/pdf` (across `GET /{url}`,
  `POST /batch`, and `/schedules`) and on explicit `?render=true` / `?wait_for=` requests
  (lazily falling back to `http_client.download()` only if Playwright fails with a non-selector
  error, without re-triggering secondary Playwright heuristics). Unified all browser entrypoints
  under `pdf_semaphore` (`HTTP 503` fail-fast on explicit `?render=true` and PDF; graceful
  static content fallback on implicit auto-detection and empty-body fallback) and removed the
  unconditional `2s` post-`networkidle` sleep in `PlaywrightPDFGenerator.generate_pdf`.
- **Decision (`/metrics*` authentication, production docs `404`, and trusted-proxy rate limits):**
  Required `Depends(get_api_key)` on `/metrics`, `/metrics/performance`, `/metrics/health-score`,
  and `/metrics/live` when `DOWNLOADER_KEY` is configured while keeping `GET /health` public;
  disabled `/docs`, `/docs/oauth2-redirect`, `/redoc`, and `/openapi.json` (`HTTP 404`) when
  `ENVIRONMENT=production` via explicit pre-router handlers ahead of `/{url:path}`; added
  `RATELIMIT_TRUSTED_PROXIES` right-to-left `X-Forwarded-For` client IP resolution and isolated
  authenticated (`key:<sha256[:16]>:<ip>`) vs unauthenticated (`unauth:<ip>`) rate-limit buckets.
- **Decision (Stateful `/batch`, `/schedules`, and Redis Lua atomicity — keeps `/schedules` metadata-only):**
  Kept `/schedules` execution-metadata-only until Sprint 6 webhook delivery while enforcing
  creation-time SSRF & custom header sanitization (`HTTP 400`), `robots.txt` + `DomainThrottle`
  compliance, non-retryable policy short-circuiting, per-attempt `SCHEDULER_MAX_WORKERS`
  concurrency (releasing slots before retry sleep), and serialized JSON `content_size` accounting.
  Upgraded `DomainThrottle` Redis slot reservation to an atomic `EVAL` Lua script and namespaced
  `RobotsChecker` caches to `robots:v2:{scheme}:{netloc}`.

---

## 2026-11-01 — `v0.6.0` Ethical Crawling, production hardening & Roadmap-first consolidation

- **Decision (Stateless-first Ethical Crawling & `Crawl-delay` — completes Sprint 4):**
  Shipped `CrawlingConfig` (`USER_AGENT`, `RESPECT_ROBOTS_TXT`, `ROBOTS_CACHE_TTL`,
  `ROBOTS_CACHE_MAX_SIZE`, `ROBOTS_FETCH_TIMEOUT`, `MAX_CRAWL_DELAY`), `RobotsChecker`
  (`src/downloader/crawling/robots.py`), and `DomainThrottle`
  (`src/downloader/crawling/throttle.py`). Both default to bounded in-memory LRU/TTL
  state (`OrderedDict` + per-domain monotonic slot locks) when `REDIS_URI` is unset
  and coordinate via Redis (`robots:v1:{netloc}`, `throttle:v1:{domain}`) when
  configured. `RobotsChecker` uses its own SSRF-validated fetcher rather than the
  route `HTTPClientDep`, fails open with cached sentinels when origins are unreachable
  or malformed, and maps disallowed URLs to `HTTP 403` (`robots_disallowed`).
- **Decision (Defense-in-depth HTTP, SSRF, Playwright, rate-limit, logging & `:8000` hardening):**
  - Enforced streaming `CONTENT_MAX_DOWNLOAD_SIZE` in `HTTPClient` (aborting oversized
    `Content-Length` headers pre-read and chunked streams mid-flight -> `HTTP 413`
    `content_too_large`) and re-validated every `3xx` redirect `Location` hop via
    `validate_url()` (`SSRFProtectionError`).
  - Added Playwright `context.route("**/*", ...)` SSRF interception across
    `pdf_generator.py` and `content_converter.py` (blocking RFC1918, loopback,
    link-local/cloud metadata `169.254.169.254`, and `file://` subresources), browser
    recycling at `PDF_MAX_USES_PER_BROWSER=50`, and idle pool scale-to-zero after
    `PDF_IDLE_TIMEOUT=600s`.
  - Activated moving-window enforcement in `RateLimitMiddleware` (`HTTP 429`
    `rate_limit_exceeded` + `Retry-After` + `X-RateLimit-*` headers, matching
    `/http...` paths to `download_limit`).
  - Extracted top-level `method`, `path`, and `status_code` fields on `uvicorn.access`
    records in `CustomJsonFormatter`, and moved the container listener from `:80` to
    unprivileged `:8000` (eliminating privileged port `sysctls`).
- **Decision (Pure Roadmap-first consolidation — supersedes `TODO.md` tracker from 2026-08-20):**
  With Sprint 4 (`S4-be-1..5`, `S4-test-1`, `S4-doc-1`) complete and `0` active open
  sprint tasks remaining, archived Sprint 4 into `docs/features/ethical-crawling.md`
  and removed root `TODO.md`. Roadmap milestones and deferred backlog items now live
  exclusively in `docs/roadmap.md`, architectural rationale in `DECISIONS.md`, and
  wire contracts in `spec/contracts.md`.

---

## 2026-11-01 — Stateless-first Sprint 4 re-alignment & offline verification gate

- **Decision (Sprint 4 vs. Sprint 5 re-ordering — supersedes 2026-01-21 roadmap order):**
  Promoted **Ethical Crawling & Stateless Safety** (`S4-be-1`..`S4-be-5`, `S4-test-1`,
  `S4-doc-1`) into Sprint 4 using a stateless-first dual-mode design: stdlib
  `urllib.robotparser` with a bounded in-memory TTL/LRU cache (1h TTL) and in-memory
  per-domain `Crawl-delay` timestamps by default when `REDIS_URI` is unset, plus
  optional Redis cache/throttle coordination when `REDIS_URI` is configured. Moved
  **Multi-Tenant Per-Key Quotas** (`QuotaConfig`, `GET /usage`, `X-RateLimit-*`,
  `429 Retry-After`) to Sprint 5 Preview (`S5-*`, deferred until multi-key or
  Redis-backed deployment).
- **Why (Stateless Kubernetes reality):** Production runs as a stateless Kubernetes
  deployment behind a single shared `DOWNLOADER_KEY` with `REDIS_URI` unset (`slowapi`
  `memory://` already enforces global endpoint rate limits). Building Redis-only
  per-key tenant accounting next would add zero value in single-key stateless pods,
  whereas stateless-compatible `robots.txt`, `USER_AGENT`, and `Crawl-delay`
  controls immediately protect live egress. If multi-tenant quotas are activated
  later, they should either target a Redis-backed deployment or implement a
  `QUOTA_STORAGE_URI → REDIS_URI → memory://` fallback chain matching `slowapi`.
  Details in `TODO.md` and `docs/roadmap.md`.
- **Decision (Lazy unified `PlaywrightPDFGenerator` pool & unblinded offline gate):**
  Unified browser lifecycle management on a lazily initialized `BrowserPool` inside
  `PlaywrightPDFGenerator` (deferring Chromium startup until the first PDF or
  rendered-HTML call rather than FastAPI lifespan startup) and hardened the default
  fast test gate with `tests/smoke/test_repo_hygiene.py` (enforcing ADR-005
  portability and repo-relative Markdown link resolution) plus deterministic
  allowlisted fixture DNS in `tests/conftest.py`.
- **Why (Fast, deterministic verification):** Lazy browser pool initialization keeps
  non-browser requests and `TestClient` suites free of multi-second Chromium spawn
  overhead, while allowlisted offline fixture DNS lets SSRF loopback/private-IP and
  mocked route tests run sub-second in network-isolated sandboxes without masking
  real defects.

---

## 2026-08-20 — Adopted gen-3 planning layout

- **Decision:** Migrated to the gen-3 planning workflow. Live status lives ONLY in
  `TODO.md`; `spec/` holds the WHAT (status-free, protected); `docs/` holds
  planning/ops (roadmap, guides, feature-history archives); this file is the
  decision log; `CRITICAL.md` registers protected paths.
- **Moves:** `PLAN.md` → `TODO.md`; `docs/api/api-reference.md` → `spec/contracts.md`.
  Task IDs normalized to lowercase feature slugs (`S4-BE-1` → `S4-be-1`).
- **Why:** One source of truth for status; separate the WHAT from the HOW;
  stop shipping dead `.claude/plans/...` links (plans are local/gitignored).
- **Cleanup:** Dead `.claude/plans/*.md` links in `docs/features/*.md` archives
  replaced with `—`; stale `product/` references in `docs/README.md` removed
  (that directory does not exist in this repo).

---

## Prior decisions (recovered from history / archives)

- **Rate-limit storage fallback chain** — `RATELIMIT_STORAGE_URI → REDIS_URI →
  memory://`. Rate limiting degrades gracefully to in-memory when Redis is absent.
  (S0-doc-5; see `.env.example`, `docs/guides/configuration.md`.)
- **503 over silent failure when Redis is down** — batch/schedule endpoints return
  a clear 503 naming the feature that requires Redis, rather than failing opaquely.
  (S0-be-1.)
- **Consistent error envelope** — `ErrorResponse` `detail` wrapping is uniform
  across routes and the OpenAPI schema matches the actual response shape. (S0-be-2.)
- **SSRF blocked by default** — `SSRF_BLOCK_PRIVATE_IPS=true`; localhost/private IPs
  and `file://` are blocked. Security posture is opt-out, not opt-in.
- **Content negotiation via `Accept` header** — one `GET /{url}` endpoint serves
  text/markdown/html/pdf/json rather than separate endpoints per format.
- **Implicit Playwright fallback in batch** — batch processing falls back to browser
  rendering when needed; documented rather than made a required explicit flag. (S0-doc-3.)

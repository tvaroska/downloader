# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

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

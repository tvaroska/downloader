# Critical Paths

Registry of protected/sensitive paths. Any task touching a path here
**auto-escalates**: stronger model + mandatory security/contract review before commit.
`spec/` is protected — agents PROPOSE changes to spec for review, never edit it unprompted.

| Path | Why it's critical |
|------|-------------------|
| `spec/` | THE WHAT — requirements/contracts. Status-free; changes are proposals, reviewed. |
| `spec/contracts.md` | Public API surface — endpoints, schemas, status codes. Breaking changes ripple to every client. |
| `src/downloader/auth.py` | API-key authentication (Bearer / X-API-Key). Auth bypass = full compromise. |
| `src/downloader/validation.py` | SSRF protection / URL & header sanitization. The core safety boundary — regressions expose internal network. |
| `src/downloader/ssrf_transport.py` | DNS-pinned `SSRFSafeNetworkBackend` & `SSRFSafeHTTPTransport` (`trust_env=False`); prevents DNS-rebinding TOCTOU at `connect_tcp`. |
| `src/downloader/http_client.py` | Redirect SSRF hop validation, custom header forwarding, and streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`) DoS boundary. |
| `src/downloader/crawling/*` | Stateless-first `robots.txt` SSRF/egress safety (`robots:v2:{scheme}:{netloc}`) and atomic per-domain `Crawl-delay` throttling. |
| `src/downloader/config.py` / `src/downloader/runtime_config.py` | Pydantic Settings (`60+` params); defines security defaults (SSRF, CORS, rate limits, trusted proxies, ethical crawling, OCR pixel caps, webhook/drain/worker/OTel limits). |
| `src/downloader/services/webhooks.py` | Outbound webhook delivery; DNS-pinned SSRF transport (`trust_env=False`, `follow_redirects=False`), HMAC-SHA256 signing, and secret redaction. |
| `src/downloader/quota/*` | Per-key quota enforcement (deferred post-`v0.7.0`). Bugs let tenants exceed limits or DoS others. |
| `src/downloader/ratelimit_middleware.py` | Moving-window rate-limit enforcement (`HTTP 429` + `Retry-After`), `RATELIMIT_TRUSTED_PROXIES`, and auth/unauth bucket isolation. |
| `src/downloader/browser/` | Playwright process isolation, max-use recycling, allowlisted declarative `actions.py` (zero JS eval), and hardening. Sandbox escape = RCE surface. |
| `src/downloader/pdf_generator.py` / `src/downloader/content_converter.py` | Unified `pdf_semaphore` browser concurrency, Playwright SSRF `context.route("**/*", ...)` guard, screenshot/PDF capture, and idle browser scale-to-zero. |
| `src/downloader/drain.py` | Graceful shutdown connection draining (`DrainCoordinator`, `DrainMiddleware`, `/health` `503` readiness transition). |
| `src/downloader/worker.py` / `src/downloader/worker_queue.py` | Split API/worker Redis `BLMOVE` job queue, remote browser RPC dispatch, and worker heartbeat lifecycle. |
| `src/downloader/ports.py` / `src/downloader/errors.py` | Hexagonal `BrowserRenderer`, `JobStore`, and `WorkQueue` contracts, single `pdf_semaphore` capacity gate, and deterministic exception-to-HTTP classification. |
| `specs/ast.rules.yaml` / `tests/test_isolation_guard.py` | Declarative AST security and port-isolation rules (`secrets.compare_digest`, framework-free `services/`, `downloader.*` import hygiene). |
| `Dockerfile` / `docker-compose.split.yml` / `docker-compose.prod.yml` | Production image (`tesseract-ocr`, Playwright Chromium, graceful drain) + deploy configs; non-root container, prod secrets. |
| `cloudbuild.yaml` / `ci/trigger.yaml` | Cloud Build CI/CD pipeline + GitHub triggers; controls verification gates and Artifact Registry release pushes. |
| `DOWNLOADER_KEY`, `REDIS_URI` (env/secrets) | Auth key + datastore credentials. Never commit real values. |

**Coverage Floor (`R80`):** Overall branch-inclusive coverage must stay `>= 83%` (enforced via `pyproject.toml`, `justfile`, `cloudbuild.yaml`, and `proj-test`), and every Python module listed in the table above must stay `>= 75%` branch-inclusive coverage (target `>= 80%` for auth, SSRF, worker, and port boundaries).

**Secrets:** `.secrets.baseline` (detect-secrets) gates commits. Regenerate with a
fresh `detect-secrets scan` when tracked files move — do not hand-edit path entries.

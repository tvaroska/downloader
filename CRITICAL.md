# Critical Paths

Registry of protected/sensitive paths. Any task touching a path here
**auto-escalates**: stronger model + mandatory security/contract review before commit.
`spec/` is protected — agents PROPOSE changes to spec for review, never edit it unprompted.

| Path | Why it's critical |
|------|-------------------|
| `spec/` | THE WHAT — requirements/contracts. Status-free; changes are proposals, reviewed. |
| `spec/contracts.md` | Public API surface — endpoints, schemas, status codes. Breaking changes ripple to every client. |
| `src/downloader/auth.py` | API-key authentication (Bearer / X-API-Key). Auth bypass = full compromise. |
| `src/downloader/validation.py` | SSRF protection / URL sanitization. The core safety boundary — regressions expose internal network. |
| `src/downloader/http_client.py` | Redirect SSRF hop validation + streaming `CONTENT_MAX_DOWNLOAD_SIZE` (`HTTP 413`) DoS boundary. |
| `src/downloader/crawling/*` | Stateless-first `robots.txt` SSRF/egress safety and per-domain `Crawl-delay` throttling. |
| `src/downloader/config.py` | Pydantic Settings (45+ params); defines security defaults (SSRF, CORS, rate limits, ethical crawling). |
| `src/downloader/quota/*` | Per-key quota enforcement (deferred Sprint 5). Bugs let tenants exceed limits or DoS others. |
| `src/downloader/ratelimit_middleware.py` | Moving-window rate-limit enforcement (`HTTP 429` + `Retry-After`) — abuse/DoS protection. |
| `src/downloader/browser/` | Playwright process isolation, max-use recycling, and hardening. Sandbox escape = RCE surface. |
| `src/downloader/pdf_generator.py` / `src/downloader/content_converter.py` | Playwright SSRF `context.route("**/*", ...)` guard and idle browser scale-to-zero. |
| `Dockerfile` / `docker-compose.prod.yml` | Production image + deploy config; non-root container, prod secrets. |
| `cloudbuild.yaml` / `ci/trigger.yaml` | Cloud Build CI/CD pipeline + GitHub triggers; controls verification gates and Artifact Registry release pushes. |
| `DOWNLOADER_KEY`, `REDIS_URI` (env/secrets) | Auth key + datastore credentials. Never commit real values. |

**Secrets:** `.secrets.baseline` (detect-secrets) gates commits. Regenerate with a
fresh `detect-secrets scan` when tracked files move — do not hand-edit path entries.

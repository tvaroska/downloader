# Critical Paths

Registry of protected/sensitive paths. Any `/implement` task touching a path here
**auto-escalates**: stronger model + mandatory review before commit. `spec/` is
protected — agents PROPOSE changes to spec, they do not edit it during `/implement`.

| Path | Why it's critical |
|------|-------------------|
| `spec/` | THE WHAT — requirements/contracts. Status-free; changes are proposals, reviewed. |
| `spec/contracts.md` | Public API surface — endpoints, schemas, status codes. Breaking changes ripple to every client. |
| `src/downloader/auth.py` | API-key authentication (Bearer / X-API-Key). Auth bypass = full compromise. |
| `src/downloader/validation.py` | SSRF protection / URL sanitization. The core safety boundary — regressions expose internal network. |
| `src/downloader/config.py` | Pydantic Settings (40+ params); defines security defaults (SSRF, rate limits). |
| `src/downloader/quota/*` | Per-key quota enforcement (Sprint 4). Bugs let tenants exceed limits or DoS others. |
| `src/downloader/ratelimit_middleware.py` | Rate-limit enforcement — abuse/DoS protection. |
| `src/downloader/browser/` | Playwright process isolation + hardening. Sandbox escape = RCE surface. |
| `Dockerfile` / `docker-compose.prod.yml` | Production image + deploy config; non-root container, prod secrets. |
| `DOWNLOADER_KEY`, `REDIS_URI` (env/secrets) | Auth key + datastore credentials. Never commit real values. |

**Secrets:** `.secrets.baseline` (detect-secrets) gates commits. Regenerate with a
fresh `detect-secrets scan` when tracked files move — do not hand-edit path entries.

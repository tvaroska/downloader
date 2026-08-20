# Decisions

Append-only log of product/technical decisions and learnings. Newest first.
Each entry: what was decided, why, and where the details live. Never rewrite
history — supersede an old decision with a new entry that references it.

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

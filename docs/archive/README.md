# Documentation Archive

**Last Updated:** 2026-01-20

This folder contains archived documentation for resolved issues and completed investigations.

## Purpose

The archive preserves historical records of:
- Bug investigations and root cause analyses
- Fix documentation and verification results
- Test findings from debugging sessions

These documents are kept for reference and learning, not as active documentation.

## Archived Issues

| Document | Issue | Status | Date |
|----------|-------|--------|------|
| [DOWNLOADER_BUG.md](DOWNLOADER_BUG.md) | Missing metadata for JS-heavy sites (Substack, Medium) | **RESOLVED** | 2026-01-02 |
| [TEST_RESULTS.md](TEST_RESULTS.md) | Test findings confirming Brotli decompression issue | **RESOLVED** | 2026-01-02 |
| [BUG_FIX_SUMMARY.md](BUG_FIX_SUMMARY.md) | Solution: Added `brotli` library dependency | **RESOLVED** | 2026-01-02 |

## Summary

All three documents relate to a single issue: **Brotli compression preventing JavaScript rendering detection**.

**Root Cause:** The `brotli` library was missing, so httpx couldn't decompress Brotli-encoded responses from modern sites like Substack.

**Fix:** `uv add brotli` - a one-line fix that enabled proper decompression and JavaScript rendering for all affected sites.

## When to Archive

Move documentation here when:
- A bug is fixed and verified
- An investigation is complete
- The information is valuable for historical reference but no longer actively maintained

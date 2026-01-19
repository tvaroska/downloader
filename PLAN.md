# REST API Downloader - Sprint Plan

**Last Updated:** 2026-01-18

## Current Sprint: Sprint 1 (Content Transformation)

**Priority:** High - Core feature for LLM use cases
**Estimated Effort:** 1 week (senior engineer)
**Focus:** Transform downloaded content into formats optimized for LLM consumption

---

## Sprint 1 - Content Transformation

### 1. HTML to Markdown (P1 - Core)

1.1. **S1-BE-1: Add markdownify dependency and core converter** ✅
   - [x] Add `markdownify>=0.11.0` to pyproject.toml
   - [x] Create `src/downloader/transformers/markdown.py`
   - [x] Implement `html_to_markdown()` with heading, list, link, code block preservation
   - Completed: 2026-01-18
   - Plan: .claude/plans/imperative-sauteeing-cook.md

1.2. **S1-BE-2: Add Accept header content negotiation to download endpoint** ✅
   - [x] Support `Accept: text/markdown`, `Accept: text/plain`, `Accept: text/html`
   - [x] Default to `text/plain` (LLM-optimized default)
   - [x] Multi-format support via comma-separated Accept values
   - Completed: 2026-01-18

1.3. **S1-BE-3: Add format option to batch endpoint** ✅
   - [x] Add `format` field to BatchURLRequest schema
   - [x] Add `default_format` field to BatchRequest schema
   - [x] Apply transformation per-URL in batch processing
   - Completed: 2026-01-18

1.4. **S1-TEST-1: Add markdown transformation tests** ✅
   - [x] Create `tests/unit/test_markdown_transformer.py`
   - [x] Test structure preservation (headings, lists, links, code blocks)
   - [x] Test edge cases (malformed HTML, empty content, nested structures)
   - Completed: 2026-01-18
   - Plan: .claude/plans/generic-scribbling-lovelace.md

### 2. Plain Text Extraction (P2 - Core)

2.1. **S1-BE-4: Implement plain text extractor** ✅
   - [x] Create `src/downloader/transformers/plaintext.py`
   - [x] Use BeautifulSoup to strip tags and extract clean text
   - [x] Handle whitespace normalization
   - Completed: 2026-01-18
   - Plan: .claude/plans/fancy-popping-origami.md

2.2. **S1-TEST-2: Add text extraction tests** ✅
   - [x] Create `tests/unit/test_plaintext_transformer.py`
   - [x] Test tag stripping, whitespace handling, Unicode support
   - Completed: 2026-01-18
   - Plan: .claude/plans/curious-yawning-storm.md

### 3. API Documentation (P1 - Required)

3.1. **S1-DOC-1: Update API reference for content negotiation** ✅
   - [x] Document `Accept` header support (`text/markdown`, `text/plain`, `text/html`)
   - [x] Add batch request format option
   - [x] Include example responses
   - [x] Add supported formats summary table
   - [x] Document multi-format requests
   - Completed: 2026-01-18
   - Plan: .claude/plans/synchronous-sniffing-ullman.md

---

## Sprint 1 Summary

| Category | Tasks | Estimated Hours |
|----------|-------|-----------------|
| Backend (Markdown) | 3 | 4-7 hours |
| Backend (Text) | 1 | 1-2 hours |
| Testing | 2 | 3 hours |
| Documentation | 1 | 1 hour |
| **Total** | **7** | **9-13 hours** |

---

## Acceptance Criteria for Sprint 1 Completion

- [ ] `Accept: text/markdown` returns clean markdown from HTML pages
- [ ] `Accept: text/plain` returns plain text with no HTML tags
- [ ] Batch endpoint supports `format` option
- [ ] Markdown preserves headings, lists, links, and code blocks
- [ ] All transformation tests pass
- [x] API documentation updated
- [ ] Transformation adds < 500ms to response time

---

## Sprint 2 Preview - OCR & Advanced Transformation

**Focus:** Image to text extraction and advanced content handling

| Task ID | Description | Priority |
|---------|-------------|----------|
| S2-BE-1 | Add pytesseract and Tesseract dependency | P1 |
| S2-BE-2 | Implement OCR transformer for images | P1 |
| S2-BE-3 | Add `Accept: image/ocr` or similar content type support | P1 |
| S2-BE-4 | Handle mixed content (HTML with embedded images) | P2 |
| S2-TEST-1 | OCR accuracy tests (>90% target) | P1 |
| S2-DOC-1 | Document OCR feature and system requirements | P1 |

---

## Completed Sprints

### Sprint 0 - Production Readiness ✅

**Completed:** 2026-01-18
**Archive:** [docs/features/production-readiness.md](docs/features/production-readiness.md)

15 tasks completed across Testing/CI, Memory/Stability, Documentation, Infrastructure, and Code Quality.

---

## Notes

- Sprint 0 remediation is complete; project is production-ready
- Content Transformation (Phase 5) is the current strategic priority
- See docs/roadmap.md for full feature roadmap
- See docs/features/ for completed work history

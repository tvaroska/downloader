# Content Transformation - Feature History

**Last Updated:** 2026-11-12

## Overview

Transform downloaded content (HTML and images) into formats optimized for downstream consumption (LLMs, data pipelines).

---

## Status: ✅ Complete (Sprint 1 + `v0.7.0` OCR)

- Sprint 1 completed: 2026-01-18 (Markdown, plain text, `Accept` negotiation)
- `v0.7.0` completed: 2026-11-12 (Tesseract image-to-text OCR with decompression-bomb guard and graceful `503` degradation)

---

## Completed Features

| Feature | Priority | Complexity | Status |
|---------|----------|------------|--------|
| HTML to Markdown | P1 | Medium | ✅ Complete |
| Plain text extraction | P2 | Low | ✅ Complete |
| Content negotiation (`Accept` header) | P1 | Medium | ✅ Complete |
| Image to Text (OCR via `pytesseract` + `Pillow`) | P2 | High | ✅ Complete (`v0.7.0`) |

### API Implemented
- `Accept` header negotiation: `text/markdown`, `text/plain`, `text/html`, `application/pdf`, `image/png`, `image/jpeg`, `application/json`
- OCR extraction: automatic on `image/*` origins (`text/plain`, `text/markdown`, `text/html`), plus `?ocr=true` and `?ocr_lang=<lang>` (`HTTP 503 ocr_unavailable` when `tesseract` binary is missing)
- Batch support: `format` (`text`, `markdown`, `html`, `pdf`, `screenshot`, `json`, `raw`) and `default_format` options in request body

### Success Criteria Met
- ✅ Markdown output preserves document structure (headings, lists, links, code blocks)
- ✅ Transformation adds < 500ms to response time (measured: 53-110ms overhead)
- ✅ Decompression-bomb pixel cap (`OCR_MAX_PIXELS=25_000_000`) and strict language code regex (`^[a-zA-Z0-9_+-]{1,32}$`)
- ✅ Transformer and OCR offline test suites passing (`tests/unit/test_v070_ocr_and_webhooks.py`)

### Dependencies Added
```
markdownify>=0.11.0
beautifulsoup4>=4.12.0  # already included
pytesseract>=0.3.13
Pillow>=11.0.0
```
System: `tesseract-ocr`, `tesseract-ocr-eng` (installed in `Dockerfile`)

---

## Completed Work

### Sprint 1 - Content Transformation (Completed 2026-01-18)

**Focus:** HTML to Markdown and Plain Text extraction for LLM use cases

#### HTML to Markdown (P1 - Core)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S1-BE-1 | Add markdownify dependency and core converter | `src/downloader/transformers/markdown.py`, `pyproject.toml` | — |
| S1-BE-2 | Add Accept header content negotiation | `src/downloader/api/routes.py` | - |
| S1-BE-3 | Add format option to batch endpoint | `src/downloader/api/routes.py`, `src/downloader/models.py` | - |
| S1-TEST-1 | Add markdown transformation tests | `tests/unit/test_markdown_transformer.py` | — |

#### Plain Text Extraction (P2 - Core)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S1-BE-4 | Implement plain text extractor | `src/downloader/transformers/plaintext.py` | — |
| S1-TEST-2 | Add text extraction tests | `tests/unit/test_plaintext_transformer.py` | — |

#### API Documentation (P1 - Required)

| Task ID | Description | Files | Plan |
|---------|-------------|-------|------|
| S1-DOC-1 | Update API reference for content negotiation | `spec/contracts.md` | — |

---

## Summary Statistics

| Category | Tasks Completed |
|----------|-----------------|
| Backend (Markdown) | 3 |
| Backend (Text) | 1 |
| Testing | 2 |
| Documentation | 1 |
| **Total** | **7** |

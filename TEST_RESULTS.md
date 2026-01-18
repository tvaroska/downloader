# HTML JavaScript Rendering Feature - Test Results

**Date:** January 2, 2026
**Feature:** JavaScript rendering for HTML responses (commit 0b76359)
**Primary Test URL:** `https://ontologist.substack.com/p/understanding-shacl-12-rules`

---

## Executive Summary

✅ **Unit Tests:** PASS (25/25 tests passed)
✅ **Integration Tests:** PASS (10/10 tests passed)
❌ **E2E Tests:** **FAIL** - Bug confirmed in production

### Key Finding

**The JavaScript rendering feature exists and works correctly in isolation, but FAILS in production for the Substack test URL.**

**Root Cause:** The service is receiving gzip-compressed HTML content (40,764 bytes) but the detection logic cannot properly parse compressed content to check for missing metadata. As a result:
- Detection does not trigger for Substack URLs
- HTML is returned without JavaScript rendering
- Metadata tags (`og:title`, `og:description`, etc.) are missing

---

## Detailed Test Results

### 1. Unit Tests - Detection Logic ✅

**File:** `tests/unit/test_html_rendering.py`
**Status:** 25/25 PASSED
**Runtime:** 0.10s

#### Test Coverage

**Metadata Detection (6 tests):**
- ✅ Detects complete metadata (og:title + og:description)
- ✅ Detects missing metadata (OpenGraph)
- ✅ Accepts Twitter Card tags as alternative
- ✅ Handles mixed OpenGraph/Twitter tags
- ✅ Detects missing title only
- ✅ Detects missing description only

**JS Framework Markers (5 tests):**
- ✅ Detects React (#root) with minimal content
- ✅ Detects Vue (#app) with minimal content
- ✅ Detects Angular (ng-app attribute)
- ✅ Ignores framework markers with substantial content (>200 chars)
- ✅ No false positives for regular HTML

**Playwright Detection (14 tests):**
- ✅ Substack domain triggers detection
- ✅ Medium domain triggers detection
- ✅ Missing metadata + small size (<50KB) triggers
- ✅ React/Vue/Angular framework markers trigger
- ✅ Explicit "enable JavaScript" messages trigger
- ✅ Static HTML with metadata does NOT trigger
- ✅ Large HTML (>50KB) with metadata does NOT trigger
- ✅ Non-HTML content does NOT trigger
- ✅ Cache behavior works correctly (both js_heavy and static)
- ✅ Malformed HTML handled gracefully

**Conclusion:** Detection logic is **correct** and comprehensive.

---

### 2. Integration Tests - Playwright Rendering ✅

**File:** `tests/integration/test_html_rendering.py`
**Status:** 10/10 PASSED
**Runtime:** 0.09s

#### Test Coverage

**Playwright HTML Rendering (7 tests):**
- ✅ Successfully renders HTML using Playwright
- ✅ Returns bytes with UTF-8 encoding
- ✅ Handles page load failures (404) gracefully
- ✅ Handles timeouts appropriately
- ✅ Attempts to close modals/popups
- ✅ Releases browser back to pool
- ✅ Handles missing browser pool initialization
- ✅ Handles null response from page.goto()

**Handler Integration (3 tests):**
- ✅ `handle_html_response()` triggers Playwright for Substack
- ✅ `handle_html_response()` skips Playwright for static HTML
- ✅ Graceful degradation on Playwright failures

**Conclusion:** Rendering logic is **correct** and robust.

---

### 3. E2E Tests - Production Service ❌

**File:** `tests/e2e/test_html_rendering_e2e.py`
**Status:** **FAILED** - Bug confirmed
**Service:** `http://localhost:8081`
**Auth:** Bearer token "value"

#### Test: Substack URL Metadata Check

**URL Tested:** `https://ontologist.substack.com/p/understanding-shacl-12-rules`

**Expected Behavior:**
- Detect as JS-heavy (Substack domain)
- Trigger Playwright rendering
- Return HTML with complete metadata
- Response size >100KB

**Actual Behavior:**
```
Response Headers:
  X-Rendered-With-JS: false  ❌ (should be "true")
  Content-Length: 40764      ❌ (should be >100KB)

HTML Analysis:
  og:title: NOT FOUND        ❌
  og:description: NOT FOUND  ❌
  og:image: NOT FOUND        ❌
  Response size: 40,764 bytes ❌ (matches bug report ~40KB)
```

**Content Analysis:**
- HTML is gzip-compressed (binary data)
- Detection logic cannot parse compressed bytes as HTML
- Metadata check fails (can't find `<meta>` tags in compressed data)
- Rendering not triggered

**Conclusion:** The bug described in DOWNLOADER_BUG.md is **CONFIRMED**.

---

## Root Cause Analysis

### Problem

The HTTP client (httpx) downloads HTML content with `Accept-Encoding: gzip, deflate, br` header, and the response is gzip-compressed. However, the detection logic in `should_use_playwright_for_html()` runs on the **compressed bytes**, not decompressed HTML.

### Evidence

1. **Response size:** 40,764 bytes (compressed)
2. **Expected size:** >200KB (decompressed + rendered)
3. **File inspection:** `/tmp/substack_response.html` contains binary gzip data
4. **Metadata search:** `grep "og:title"` returns 0 results

### Code Flow Issue

```
HTTP Client (httpx)
  ↓
Downloads compressed HTML (40KB gzip)
  ↓
content_processor.py:handle_html_response()
  ↓
should_use_playwright_for_html(url, content, content_type)
  ← content is STILL COMPRESSED bytes
  ↓
BeautifulSoup(content, "html.parser")  ← FAILS to parse binary gzip
  ↓
Detection fails → returns False
  ↓
No Playwright rendering triggered
```

### Expected Flow

```
HTTP Client (httpx)
  ↓
Downloads HTML
  ↓
**DECOMPRESS if gzipped**  ← MISSING STEP
  ↓
Detection runs on decompressed HTML
  ↓
Properly detects missing metadata
  ↓
Triggers Playwright rendering
```

---

## Recommendations

### Immediate Fix (High Priority)

**Location:** `src/downloader/http_client.py` or `src/downloader/services/content_processor.py`

**Option 1:** Ensure httpx automatically decompresses content
```python
# In http_client.py
async def _do_download(self, url: str, circuit_breaker: CircuitBreaker):
    response = await self._client.get(url)

    # httpx should automatically decompress, but verify:
    content = response.content  # Should be decompressed

    # OR explicitly decode:
    content = response.read()  # Forces decompression
```

**Option 2:** Decompress before detection
```python
# In content_processor.py or content_converter.py
def should_use_playwright_for_html(url: str, content: bytes, content_type: str):
    # Decompress if gzipped
    if content[:2] == b'\x1f\x8b':  # gzip magic bytes
        import gzip
        content = gzip.decompress(content)

    # Now run detection on decompressed HTML
    soup = BeautifulSoup(content, "html.parser")
    ...
```

### Verification Steps

1. Add decompression logic
2. Re-run E2E test:
   ```bash
   pytest tests/e2e/test_html_rendering_e2e.py::TestHTMLRenderingE2E::test_substack_url_returns_complete_metadata -v -s
   ```
3. Verify:
   - `X-Rendered-With-JS: true` header
   - Response size >100KB
   - `og:title` metadata present

---

## Test Files Created

All tests follow existing patterns and can be integrated into CI/CD:

1. **`tests/fixtures/html_fixtures.py`** - HTML content fixtures
   - Substack-like minimal HTML
   - Complete HTML with metadata
   - React/Vue/Angular app HTML
   - Various edge cases

2. **`tests/unit/test_html_rendering.py`** - Unit tests (25 tests)
   - Metadata detection logic
   - JS framework marker detection
   - Playwright detection triggers
   - Cache behavior

3. **`tests/integration/test_html_rendering.py`** - Integration tests (10 tests)
   - Playwright rendering with mocks
   - Handler integration
   - Error handling

4. **`tests/e2e/test_html_rendering_e2e.py`** - E2E tests (network required)
   - Real Substack URL testing
   - Static URL comparison
   - Metadata verification

---

## Summary

**Feature Status:** ✅ Implemented correctly in code
**Production Status:** ❌ Not working due to compression issue
**Test Coverage:** ✅ Comprehensive (35 automated tests)
**Issue:** Gzip compression prevents HTML parsing for metadata detection
**Fix Complexity:** Low (add decompression step)
**Priority:** High (affects all JS-heavy sites: Substack, Medium, etc.)

The JavaScript rendering feature is **well-designed and correctly implemented**, but requires a **simple fix** to handle compressed HTTP responses before running detection logic.

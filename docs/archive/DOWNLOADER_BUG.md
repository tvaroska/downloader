# Downloader Service Issue: Missing Metadata for JavaScript-Heavy Sites

> **Status: RESOLVED** - Fixed by adding `brotli` library. See [BUG_FIX_SUMMARY.md](BUG_FIX_SUMMARY.md).

**Service:** [github.com/tvaroska/downloader](https://github.com/tvaroska/downloader)
**Issue Date:** January 2, 2026
**Severity:** Medium - Affects content quality but has workarounds
**Status:** Resolved

---

## Executive Summary

The downloader service at `http://localhost:8081` successfully returns HTML content for JavaScript-heavy websites (e.g., Substack articles), but the returned HTML is missing critical metadata tags like `<title>` and `<meta property="og:title">`. This results in incomplete content extraction and degraded user experience in applications relying on the service.

---

## Problem Description

### Current Behavior

When requesting HTML content from the downloader service:

**Request:**
```bash
curl -H "Accept: text/html" \
     -H "Authorization: Bearer <token>" \
     http://localhost:8081/https://ontologist.substack.com/p/understanding-shacl-12-rules
```

**Response:**
- **Status:** `200 OK` ✓
- **Content-Type:** `text/html; charset=utf-8` ✓
- **Content-Length:** `~38KB - 40KB`
- **Missing Tags:**
  - `<title>` tag (empty or absent)
  - `<meta property="og:title">`
  - `<meta property="og:description">`
  - `<meta property="og:image">`
  - Other critical metadata tags

### Expected Behavior

When fetching the same URL directly with a standard HTTP client:

**Direct Fetch:**
```python
import httpx
response = httpx.get(url, headers={'User-Agent': 'Mozilla/5.0'})
```

**Response:**
- **Status:** `200 OK`
- **Content-Length:** `~236KB` (6x larger than downloader response)
- **Contains Complete Metadata:**
  ```html
  <title data-rh="true">Understanding SHACL 1.2 Rules - by Kurt Cagle</title>
  <meta property="og:title" content="Understanding SHACL 1.2 Rules">
  <meta property="og:description" content="Adding an inferencing layer to SHACL">
  <meta property="og:image" content="https://substackcdn.com/image/fetch/...">
  ```

---

## Root Cause Analysis

### Why This Happens

Based on service logs and behavior:

1. **Browser Pool Used Only for PDF Generation**
   - Service logs show: `PDF generator initialized with browser pool`
   - Browser pool has 3 Playwright instances ready
   - However, for HTML requests (`Accept: text/html`), the service uses HTTP client instead

2. **No JavaScript Execution for HTML Responses**
   - Substack uses server-side rendering + heavy JavaScript
   - The downloader returns the initial HTML payload before JavaScript executes
   - Metadata tags are dynamically inserted/modified by React hydration
   - Service returns too early before DOM is fully constructed

3. **Content Size Discrepancy**
   - Downloader: 38-40KB (incomplete HTML)
   - Direct fetch: 236KB (complete rendered HTML)
   - Missing ~200KB suggests JavaScript bundles and dynamic content not loaded

### Service Log Evidence

```
2026-01-02 20:05:28 - src.downloader.routes.download - INFO - Processing download request for: https://ontologist.substack.com/p/understanding-shacl-12-rules
2026-01-02 20:05:28 - src.downloader.http_client - INFO - Starting download from: https://ontologist.substack.com/p/understanding-shacl-12-rules
2026-01-02 20:05:28 - src.downloader.http_client - INFO - Download completed: 40767 bytes, status: 200, type: text/html; charset=utf-8, http_version: HTTP/2
2026-01-02 20:05:28 - src.downloader.routes.download - INFO - Requested format: html (Accept: text/html)
```

Notice: `http_client` is used, not the browser pool.

---

## Impact

### Downstream Effects

Applications using the downloader service for content extraction experience:

1. **Missing Article Titles**
   - Articles fall back to displaying URLs instead of proper titles
   - Degrades UX and content discoverability

2. **Missing Descriptions**
   - No preview text available for article cards
   - Reduces content quality in feeds/lists

3. **Missing Featured Images**
   - No OpenGraph images for social sharing
   - Less engaging UI without thumbnails

4. **Silent Degradation**
   - Service returns `200 OK` with incomplete data
   - No error to trigger fallback logic
   - Difficult to detect without content inspection

### Affected Sites

Confirmed to affect:
- **Substack articles** (all tested URLs)
- **Likely affected:** Medium, modern blogs, JavaScript-heavy news sites

Sites that work fine:
- Static HTML pages
- Older CMS platforms (WordPress, etc.)

---

## Test Case

### URL to Test
```
https://ontologist.substack.com/p/understanding-shacl-12-rules
```

### Expected Metadata Extraction

The service should return HTML containing these tags:

```json
{
  "title": "Understanding SHACL 1.2 Rules - by Kurt Cagle",
  "og:title": "Understanding SHACL 1.2 Rules",
  "og:description": "Adding an inferencing layer to SHACL",
  "og:image": "https://substackcdn.com/image/fetch/s_1200x600,c_fill,f_jpg,q_auto:good,fl_progressive:steep,g_auto/https%3A%2F%2Fsubstack-post-media.s3.amazonaws.com%2Fpublic%2Fimages%2F523b88c0-0175-4822-8e20-6ce68716e7b9_2688x1536.jpeg",
  "description": "Adding an inferencing layer to SHACL",
  "author": "Kurt Cagle"
}
```

### Verification Script

```bash
#!/bin/bash
# Test if downloader returns complete metadata

RESPONSE=$(curl -s -H "Accept: text/html" \
  -H "Authorization: Bearer $TOKEN" \
  http://localhost:8081/https://ontologist.substack.com/p/understanding-shacl-12-rules)

# Check for metadata tags
echo "Checking for metadata tags..."
echo "$RESPONSE" | grep -q 'property="og:title"' && echo "✓ og:title found" || echo "✗ og:title missing"
echo "$RESPONSE" | grep -q 'property="og:description"' && echo "✓ og:description found" || echo "✗ og:description missing"
echo "$RESPONSE" | grep -q 'property="og:image"' && echo "✓ og:image found" || echo "✗ og:image missing"
echo "$RESPONSE" | grep -q '<title' && echo "✓ title tag found" || echo "✗ title tag missing"

# Check content size
SIZE=$(echo "$RESPONSE" | wc -c)
echo "Response size: ${SIZE} bytes"
[ $SIZE -gt 100000 ] && echo "✓ Size looks good" || echo "✗ Size too small (expected >100KB)"
```

---

## Proposed Solutions

### Option 1: Use Browser Pool for HTML Requests (Recommended)

**Approach:**
- Route HTML requests through the Playwright browser pool
- Wait for metadata tags to load before returning HTML
- Use the same rendering logic currently used for PDF generation

**Implementation:**
```javascript
// Pseudocode
async function handleHTMLRequest(url) {
  const browser = await browserPool.acquire();
  const page = await browser.newPage();

  try {
    await page.goto(url, { waitUntil: 'networkidle' });

    // Wait for critical metadata
    await page.waitForSelector('title', { timeout: 5000 });
    await page.waitForSelector('meta[property="og:title"]', { timeout: 5000 });

    const html = await page.content();
    return html;
  } finally {
    await browserPool.release(browser);
  }
}
```

**Pros:**
- Complete solution for JavaScript-heavy sites
- Reuses existing browser pool infrastructure
- Consistent rendering quality

**Cons:**
- Higher resource usage (CPU, memory)
- Slower response times (need to render page)
- May require rate limiting/queuing

### Option 2: Hybrid Approach

**Approach:**
- Try HTTP client first (fast path)
- If metadata tags missing, fall back to browser rendering
- Cache which URLs need browser rendering

**Implementation:**
```javascript
async function fetchContent(url) {
  // Try fast HTTP fetch first
  const httpResponse = await httpClient.get(url);

  // Check if metadata is present
  if (hasRequiredMetadata(httpResponse.body)) {
    return httpResponse.body;
  }

  // Fall back to browser rendering
  return await renderWithBrowser(url);
}
```

**Pros:**
- Fast for simple pages
- Complete for complex pages
- Optimized resource usage

**Cons:**
- More complex implementation
- Two code paths to maintain

### Option 3: Configuration Flag

**Approach:**
- Add `render_js` query parameter or header
- Let clients choose rendering method
- Default to HTTP client for backward compatibility

**Usage:**
```bash
# Force browser rendering
curl http://localhost:8081/https://example.com?render_js=true

# Or via header
curl -H "X-Render-JavaScript: true" http://localhost:8081/https://example.com
```

**Pros:**
- Backward compatible
- Client controls performance/quality tradeoff
- Flexible

**Cons:**
- Requires client-side logic
- Clients need to know which URLs need rendering
- More API surface to document

---

## Recommended Solution

**Implement Option 1 (Browser rendering for HTML) with Option 3 (configuration flag)**

1. **Default behavior:** Use browser pool for HTML requests
2. **Add flag:** `render_js=false` to disable for simple pages (performance optimization)
3. **Add timeout:** Configurable timeout for waiting on metadata (default: 5s)
4. **Add retry:** If metadata missing after timeout, warn but return what we have

This ensures quality by default while allowing performance optimization for known-simple URLs.

---

## Workaround (Current)

Until the service is fixed, downstream applications can:

1. **Detect missing metadata** in responses
2. **Fall back to direct HTTP fetch** when metadata is absent
3. **Cache successful extractions** to avoid repeated fetches

**Example workaround code:**
```python
async def fetch_with_fallback(url: str):
    # Try downloader service first
    response = await downloader_service.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    # Check if metadata is present
    if not soup.find('meta', property='og:title'):
        # Fall back to direct fetch
        direct_response = await httpx.get(url, headers={'User-Agent': 'Mozilla/5.0'})
        return direct_response.text

    return response.text
```

---

## Additional Context

### Service Configuration

```
Downloader Service URL: http://localhost:8081
Browser pool: 3 Playwright instances (Chromium)
Max download size: 50MB
Rate limiting: enabled
Authentication: enabled (Bearer token)
Redis: disabled
```

### Environment

- **Container:** `ghcr.io/tvaroska/downloader:latest`
- **Service uptime:** Started ~1 hour ago
- **Browser pool status:** Healthy (3/3 browsers initialized)
- **Logs show:** HTTP client used for HTML requests, not browser pool

### Related Features

The service supports:
- PDF generation (uses browser pool) ✓
- Markdown conversion ✓
- HTML fetching ✗ (incomplete for JS sites)

---

## Questions for Maintainers

1. Is the browser pool intended only for PDF generation, or should it be used for HTML requests too?
2. Would you prefer Option 1, 2, or 3 for the fix?
3. Are there performance concerns with using the browser pool for all HTML requests?
4. Should we add a content size threshold to trigger browser rendering?
5. Is there a way to detect if a URL needs JavaScript rendering before fetching?

---

## References

- **Downloader repository:** https://github.com/tvaroska/downloader
- **Related issue:** (to be filled in after creating GitHub issue)
- **Test URLs:**
  - Substack: https://ontologist.substack.com/p/understanding-shacl-12-rules
  - (Add more problematic URLs as discovered)

---

**Report prepared by:** Content Management Application Development Team
**Date:** January 2, 2026
**Contact:** (Add your contact info if sharing externally)

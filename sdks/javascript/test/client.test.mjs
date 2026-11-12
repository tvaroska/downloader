import assert from "node:assert/strict";
import test from "node:test";
import { DownloaderApiError, DownloaderClient } from "../dist/index.js";

function createMockFetch() {
  return async (input, init = {}) => {
    const url = new URL(String(input));
    const headers = init.headers ?? {};
    assert.equal(headers["X-API-Key"], "secret-123");

    if (url.pathname === "/health") {
      return new Response(
        JSON.stringify({ status: "healthy", version: "0.7.0", environment: "test" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/render" && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      assert.equal(body.output, "markdown");
      assert.equal(body.actions[0].type, "click");
      return new Response("# Rendered DOM", {
        status: 200,
        headers: {
          "content-type": "text/markdown; charset=utf-8",
          "x-rendered-with-js": "true",
          "x-trace-id": "abcdef123456", // pragma: allowlist secret
        },
      });
    }

    if (url.pathname === "/https://example.com/shot") {
      assert.equal(url.searchParams.get("output"), "screenshot");
      assert.equal(url.searchParams.get("full_page"), "true");
      return new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), {
        status: 200,
        headers: { "content-type": "image/png" },
      });
    }

    if (url.pathname === "/https://example.com/scan.png") {
      assert.equal(url.searchParams.get("ocr"), "true");
      assert.equal(url.searchParams.get("ocr_lang"), "eng");
      return new Response("Scanned text", {
        status: 200,
        headers: { "content-type": "text/plain", "x-ocr-applied": "true" },
      });
    }

    if (url.pathname === "/batch" && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      assert.equal(body.webhook_url, "https://hooks.example.com/cb");
      return new Response(
        JSON.stringify({
          job_id: "job-js-1",
          status: "pending",
          created_at: "2026-11-12T00:00:00Z",
          total_urls: 1,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/jobs/job-js-1/status") {
      return new Response(
        JSON.stringify({
          job_id: "job-js-1",
          status: "completed",
          progress: 100,
          created_at: "2026-11-12T00:00:00Z",
          total_urls: 1,
          processed_urls: 1,
          successful_urls: 1,
          failed_urls: 0,
          results_available: true,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/jobs/job-js-1/results") {
      return new Response(
        JSON.stringify({
          job_id: "job-js-1",
          status: "completed",
          total_duration: 0.2,
          results: [{ url: "https://example.com", success: true }],
          summary: { total_requests: 1, successful_requests: 1 },
          created_at: "2026-11-12T00:00:00Z",
          completed_at: "2026-11-12T00:00:01Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/https://rate.limit") {
      return new Response(
        JSON.stringify({
          detail: { error: "Rate limit exceeded", error_type: "rate_limit_exceeded" },
        }),
        {
          status: 429,
          headers: { "content-type": "application/json", "retry-after": "10" },
        },
      );
    }

    return new Response("fallback", { status: 200 });
  };
}

test("DownloaderClient handles download, render, OCR, screenshot, batch, and API errors", async () => {
  const client = new DownloaderClient({
    baseUrl: "http://mock.local",
    apiKey: "secret-123", // pragma: allowlist secret
    fetch: createMockFetch(),
  });

  const health = await client.health();
  assert.equal(health.version, "0.7.0");

  const shot = await client.screenshot("https://example.com/shot", { fullPage: true });
  assert.equal(shot.contentType, "image/png");
  assert.equal(shot.bytes[0], 0x89);

  const ocr = await client.ocr("https://example.com/scan.png", { language: "eng" });
  assert.equal(ocr.text(), "Scanned text");
  assert.equal(ocr.ocrApplied, true);

  const rendered = await client.render("https://example.com", {
    output: "markdown",
    actions: [{ type: "click", selector: "#load" }],
  });
  assert.equal(rendered.text(), "# Rendered DOM");
  assert.equal(rendered.renderedWithJs, true);
  assert.equal(rendered.traceId, "abcdef123456"); // pragma: allowlist secret

  const batch = await client.submitBatch(["https://example.com"], {
    webhookUrl: "https://hooks.example.com/cb",
  });
  const results = await client.waitForBatch(batch.job_id, { pollIntervalMs: 5 });
  assert.equal(results.summary.successful_requests, 1);

  await assert.rejects(
    () => client.download("https://rate.limit"),
    (err) => {
      assert.ok(err instanceof DownloaderApiError);
      assert.equal(err.statusCode, 429);
      assert.equal(err.errorType, "rate_limit_exceeded");
      assert.equal(err.retryAfter, 10);
      return true;
    },
  );
});

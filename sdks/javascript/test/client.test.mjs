import assert from "node:assert/strict";
import test from "node:test";
import {
  DownloaderApiError,
  DownloaderClient,
  encodePathSegment,
  encodeTargetUrlPath,
} from "../src/index.js";

function createMockFetch() {
  return async (input, init = {}) => {
    const url = new URL(String(input));
    const headers = init.headers ?? {};
    assert.equal(headers["X-API-Key"], "secret-123");

    if (url.pathname === "/health") {
      return new Response(
        JSON.stringify({ status: "healthy", version: "0.8.3", environment: "test" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/render" && init.method === "POST") {
      const body = JSON.parse(String(init.body));
      assert.equal(body.output, "markdown");
      assert.equal(body.actions[0].type, "click");
      if (body.timeout_ms !== undefined) {
        assert.equal(body.timeout_ms, 5000);
      }
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

    if (url.pathname === "/jobs/job-cancelled/status") {
      return new Response(
        JSON.stringify({
          job_id: "job-cancelled",
          status: "cancelled",
          progress: 50,
          created_at: "2026-11-12T00:00:00Z",
          total_urls: 2,
          processed_urls: 1,
          successful_urls: 1,
          failed_urls: 0,
          results_available: false,
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

    if (url.pathname === "/jobs/job-js-1" && init.method === "DELETE") {
      return new Response(
        JSON.stringify({ success: true, message: "Job job-js-1 cancelled successfully" }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/schedules/sched-js-1" && init.method === "GET") {
      return new Response(
        JSON.stringify({
          id: "sched-js-1",
          name: "Hourly",
          url: "https://example.com",
          cron_expression: "0 * * * *",
          format: "text",
          enabled: true,
          created_at: "2026-11-12T00:00:00Z",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/schedules/sched-js-1/history" && init.method === "GET") {
      assert.equal(url.searchParams.get("limit"), "10");
      assert.equal(url.searchParams.get("offset"), "2");
      return new Response(
        JSON.stringify({
          executions: [
            {
              execution_id: "exec-js-1",
              schedule_id: "sched-js-1",
              status: "completed",
              started_at: "2026-11-12T09:00:00Z",
              completed_at: "2026-11-12T09:00:01Z",
              duration: 1.0,
              success: true,
              content_size: 256,
              attempt: 1,
            },
          ],
          total: 17,
          limit: 10,
          offset: 2,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }

    if (url.pathname === "/https://example.com/multi") {
      const accept = String(headers.Accept ?? "");
      if (accept.includes(",")) {
        return new Response(
          JSON.stringify({
            url: "https://example.com/multi",
            formats: { text: "Hello", markdown: "# Hello" },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      }
      return new Response("Single format body", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
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
  assert.equal(health.version, "0.8.3");

  const shot = await client.screenshot("https://example.com/shot", { fullPage: true });
  assert.equal(shot.contentType, "image/png");
  assert.equal(shot.bytes[0], 0x89);

  const ocr = await client.ocr("https://example.com/scan.png", { language: "eng" });
  assert.equal(ocr.text(), "Scanned text");
  assert.equal(ocr.ocrApplied, true);

  const rendered = await client.render("https://example.com", {
    output: "markdown",
    actions: [{ type: "click", selector: "#load" }],
    timeoutMs: 5000,
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
    () => client.waitForBatch("job-cancelled", { pollIntervalMs: 5 }),
    (err) => {
      assert.ok(err instanceof DownloaderApiError);
      assert.equal(err.statusCode, null);
      assert.equal(err.errorType, "batch_cancelled");
      return true;
    },
  );

  const cancelled = await client.cancelJob("job-js-1");
  assert.equal(cancelled.success, true);

  const sched = await client.getSchedule("sched-js-1");
  assert.equal(sched.name, "Hourly");

  const history = await client.getScheduleHistory("sched-js-1", { limit: 10, offset: 2 });
  assert.equal(history.total, 17);
  assert.equal(history.limit, 10);
  assert.equal(history.offset, 2);
  assert.equal(history.executions[0].execution_id, "exec-js-1");

  const multi = await client.downloadMulti("https://example.com/multi", ["text", "markdown"]);
  assert.deepEqual(multi.formats, { text: "Hello", markdown: "# Hello" });

  await assert.rejects(
    () => client.downloadMulti("https://example.com/multi", ["text"]),
    (err) => {
      assert.ok(err instanceof DownloaderApiError);
      assert.equal(err.errorType, "unexpected_content_type");
      return true;
    },
  );

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

test("download percent-encodes target %, ?, and # without producing duplicate ?", async () => {
  let capturedInput = "";
  const client = new DownloaderClient({
    baseUrl: "http://mock.local",
    apiKey: "secret-123", // pragma: allowlist secret
    fetch: async (input) => {
      capturedInput = String(input);
      return new Response("search results", {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    },
  });

  assert.equal(
    encodeTargetUrlPath("https://example.com/search?q=hello%20world&page=2#top"),
    "https://example.com/search%3Fq=hello%2520world&page=2%23top",
  );
  assert.equal(encodePathSegment("job/1?x=2", "jobId"), "job%2F1%3Fx%3D2");
  assert.throws(() => encodePathSegment("..", "jobId"), /must be a non-empty path segment/);

  const res = await client.download("https://example.com/search?q=hello%20world&page=2#top", {
    render: true,
  });
  assert.equal(res.text(), "search results");
  assert.equal(
    capturedInput,
    "http://mock.local/https://example.com/search%3Fq=hello%2520world&page=2%23top?render=true",
  );
  const parsed = new URL(capturedInput);
  assert.equal(parsed.pathname, "/https://example.com/search%3Fq=hello%2520world&page=2%23top");
  assert.equal(parsed.search, "?render=true");
  assert.equal(parsed.hash, "");
});

import {
  type DownloadOptions,
  type JobCancelResult,
  type RenderOptions,
  type ScheduleHistory,
  DownloaderApiError,
  DownloaderClient,
  encodePathSegment,
  encodeTargetUrlPath,
} from "../src/index.js";

const encoded: string = encodeTargetUrlPath("https://example.com/search?q=a%20b#top");
const encodedId: string = encodePathSegment("job/1", "jobId");
const opts: DownloadOptions = { format: "markdown", render: true };
const renderOpts: RenderOptions = { output: "markdown", timeoutMs: 5000 };
const client = new DownloaderClient({ baseUrl: "http://localhost:8000" });
const err = new DownloaderApiError("bad", 400, "validation_error", null);
const batchErr = new DownloaderApiError("cancelled", null, "batch_cancelled", null);
const renderPromise = client.render("https://example.com", renderOpts);
const multiPromise: Promise<Record<string, unknown>> = client.downloadMulti(
  "https://example.com",
  ["text", "markdown"],
);
const cancelPromise: Promise<JobCancelResult> = client.cancelJob("job-1");
const historyPromise: Promise<ScheduleHistory> = client.getScheduleHistory("sched-1", {
  limit: 10,
  offset: 0,
});

void encoded;
void encodedId;
void opts;
void renderOpts;
void client;
void err;
void batchErr;
void renderPromise;
void multiPromise;
void cancelPromise;
void historyPromise;

import {
  type DownloadOptions,
  type JobCancelResult,
  type ScheduleHistory,
  DownloaderApiError,
  DownloaderClient,
  encodeTargetUrlPath,
} from "../src/index.js";

const encoded: string = encodeTargetUrlPath("https://example.com/search?q=a%20b#top");
const opts: DownloadOptions = { format: "markdown", render: true };
const client = new DownloaderClient({ baseUrl: "http://localhost:8000" });
const err = new DownloaderApiError("bad", 400, "validation_error", null);
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
void opts;
void client;
void err;
void multiPromise;
void cancelPromise;
void historyPromise;

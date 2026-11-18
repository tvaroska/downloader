import {
  type DownloadOptions,
  DownloaderApiError,
  DownloaderClient,
  encodeTargetUrlPath,
} from "../src/index.js";

const encoded: string = encodeTargetUrlPath("https://example.com/search?q=a%20b#top");
const opts: DownloadOptions = { format: "markdown", render: true };
const client = new DownloaderClient({ baseUrl: "http://localhost:8000" });
const err = new DownloaderApiError("bad", 400, "validation_error", null);

void encoded;
void opts;
void client;
void err;

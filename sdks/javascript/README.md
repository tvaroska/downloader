# Downloader JavaScript / TypeScript SDK (`@downloader/client`)

Zero-dependency ESM + TypeScript client using standard Web `fetch` (Node 18+, Bun, Deno, or browsers).

## Usage

```ts
import { DownloaderClient } from "@downloader/client";

const client = new DownloaderClient({
  baseUrl: "http://localhost:8000",
  apiKey: process.env.DOWNLOADER_KEY,
});

const page = await client.download("https://example.com", { format: "markdown" });
console.log(page.text());

const shot = await client.screenshot("https://example.com", {
  fullPage: true,
  actions: [{ type: "click", selector: "#accept" }],
});

const rendered = await client.render("https://example.com", {
  output: "markdown",
  waitFor: "article",
  actions: [{ type: "scroll", y: 600 }],
});

const batch = await client.submitBatch(["https://example.com"], {
  defaultFormat: "markdown",
  webhookUrl: "https://hooks.example.com/done",
  webhookSecret: process.env.WEBHOOK_SIGNING_SECRET,
});
const results = await client.waitForBatch(batch.job_id);
```

## Testing

```bash
node --test test/client.test.mjs
```

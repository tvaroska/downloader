# Downloader Python SDK (`downloader-sdk`)

Typed synchronous (`DownloaderClient`) and asynchronous (`AsyncDownloaderClient`) client for the REST API Downloader `v0.7.0+` API.

## Installation

```bash
pip install ./sdks/python
# or with uv:
uv pip install ./sdks/python
```

## Quick Start

```python
import os
from downloader_sdk import BrowserAction, DownloaderClient

with DownloaderClient("http://localhost:8000", api_key=os.environ["DOWNLOADER_KEY"]) as client:
    # Markdown download
    page = client.download("https://example.com", format="markdown")
    print(page.text)

    # Full-page PNG screenshot after clicking a cookie banner
    shot = client.screenshot(
        "https://example.com",
        full_page=True,
        actions=[BrowserAction(type="click", selector="#accept-cookies")],
    )
    with open("page.png", "wb") as f:
        f.write(shot.content)

    # Structured POST /render
    rendered = client.render(
        "https://example.com",
        output="markdown",
        wait_for="main",
        actions=[BrowserAction(type="scroll", y=800)],
    )

    # Image OCR extraction
    scan = client.ocr("https://example.com/invoice.png", language="eng")
    print(scan.text)

    # Async batch job with webhook notification
    job = client.submit_batch(
        ["https://example.com", "https://example.org"],
        default_format="markdown",
        webhook_url="https://hooks.example.com/downloader",
        webhook_secret=os.environ["WEBHOOK_SIGNING_SECRET"],
    )
    results = client.wait_for_batch(job.job_id)
    print(results.summary)
```

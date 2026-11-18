#!/usr/bin/env python3
"""Batch processing example submitting and saving multi-URL batches."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("batch_outputs")

EXAMPLE_URLS = [
    {"url": "https://example.com", "format": "text"},
    {"url": "https://httpbin.org/html", "format": "markdown"},
    {"url": "https://github.com/python/cpython", "format": "text"},
    {"url": "https://docs.python.org/3/library/asyncio.html", "format": "markdown"},
    {"url": "https://fastapi.tiangolo.com/", "format": "html"},
]

PDF_URLS = [
    {"url": "https://example.com", "format": "pdf"},
    {"url": "https://httpbin.org/html", "format": "pdf"},
]


async def check_server_health() -> bool:
    """Check if the server is running and healthy."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)
            return response.status_code == 200
    except (httpx.HTTPError, OSError) as exc:
        print(f"Cannot connect to server at {BASE_URL}: {exc}")
        return False


async def process_batch_request(
    urls: list[dict[str, str]],
    batch_name: str,
    **kwargs: Any,
) -> dict[str, Any]:
    """Submit a batch request and poll until results are ready."""
    batch_request = {
        "urls": urls,
        "default_format": "text",
        "concurrency_limit": 5,
        "timeout_per_url": 30,
        **kwargs,
    }
    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            submit_resp = await client.post(f"{BASE_URL}/batch", json=batch_request)
            if submit_resp.status_code != 200:
                return {"error": submit_resp.text, "status_code": submit_resp.status_code}
            job_id = submit_resp.json()["job_id"]
            while True:
                status_resp = await client.get(f"{BASE_URL}/jobs/{job_id}/status")
                if status_resp.status_code != 200:
                    return {"error": status_resp.text, "status_code": status_resp.status_code}
                state = status_resp.json()["status"]
                if state == "completed":
                    res_resp = await client.get(f"{BASE_URL}/jobs/{job_id}/results")
                    data: dict[str, Any] = res_resp.json()
                    print(f"Batch '{batch_name}' finished in {time.time() - start_time:.2f}s")
                    return data
                if state in {"failed", "cancelled"}:
                    return {"error": f"Job ended in state {state}"}
                await asyncio.sleep(1.0)
    except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
        return {"error": str(exc)}


def save_batch_results(batch_data: dict[str, Any], batch_name: str) -> None:
    """Save batch results to disk."""
    if "error" in batch_data or "results" not in batch_data:
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_file = OUTPUT_DIR / f"{batch_name}_{timestamp}.json"
    json_file.write_text(json.dumps(batch_data, indent=2), encoding="utf-8")

    ext_map = {"text": "txt", "markdown": "md", "html": "html", "json": "json", "pdf": "pdf"}
    for i, result in enumerate(batch_data["results"], 1):
        if not result.get("success"):
            continue
        ext = ext_map.get(str(result.get("format")), "txt")
        url_slug = "".join(
            c
            for c in str(result["url"]).removeprefix("https://").removeprefix("http://")
            if c.isalnum() or c in "._-"
        )[:50]
        content_file = OUTPUT_DIR / f"{batch_name}_{i:02d}_{url_slug}.{ext}"
        if result.get("content_base64"):
            content_file.write_bytes(base64.b64decode(result["content_base64"]))
        elif result.get("content") is not None:
            content_file.write_text(str(result["content"]), encoding="utf-8")


async def main() -> None:
    """Run basic and PDF batch demonstrations."""
    if not await check_server_health():
        return

    basic_data = await process_batch_request(EXAMPLE_URLS, "basic_demo", concurrency_limit=3)
    save_batch_results(basic_data, "basic_demo")

    pdf_data = await process_batch_request(
        PDF_URLS,
        "pdf_demo",
        default_format="pdf",
        concurrency_limit=2,
        timeout_per_url=60,
    )
    save_batch_results(pdf_data, "pdf_demo")


if __name__ == "__main__":
    asyncio.run(main())

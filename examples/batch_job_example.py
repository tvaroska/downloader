#!/usr/bin/env python3
"""Example: Using the job-based batch processing API."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
POLL_INTERVAL = 2


async def submit_batch_job(urls_with_formats: list[dict[str, str]]) -> str | None:
    """Submit a batch processing job."""
    request_data = {
        "urls": urls_with_formats,
        "default_format": "text",
        "concurrency_limit": 10,
        "timeout_per_url": 30,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(f"{BASE_URL}/batch", json=request_data)
        if response.status_code != 200:
            print(f"Failed to submit job: {response.status_code}\n{response.text}")
            return None

        result = response.json()
        job_id = str(result["job_id"])
        print(f"Job submitted: id={job_id} status={result['status']} urls={result['total_urls']}")
        return job_id


async def get_job_status(job_id: str) -> dict[str, Any] | None:
    """Get the current status of a job."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/jobs/{job_id}/status")
        if response.status_code != 200:
            print(f"Failed to get job status: {response.status_code}")
            return None
        return response.json()


async def download_job_results(job_id: str) -> dict[str, Any] | None:
    """Download the results of a completed job."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/jobs/{job_id}/results")
        if response.status_code != 200:
            print(f"Failed to download results: {response.status_code}\n{response.text}")
            return None
        return response.json()


async def wait_for_completion(job_id: str) -> dict[str, Any] | None:
    """Poll job status until completion."""
    start_time = time.time()
    while True:
        status_info = await get_job_status(job_id)
        if not status_info:
            return None

        elapsed = time.time() - start_time
        status = status_info["status"]
        progress = status_info["progress"]
        print(
            f"   [{elapsed:6.1f}s] Status: {status:10} | Progress: {progress:3}% | "
            f"Processed: {status_info['processed_urls']}/{status_info['total_urls']}"
        )

        if status in {"completed", "failed", "cancelled"}:
            return status_info
        await asyncio.sleep(POLL_INTERVAL)


def _print_results(results: dict[str, Any]) -> None:
    summary = results["summary"]
    print(
        f"\nResults Summary: total={summary['total_requests']} "
        f"ok={summary['successful_requests']} failed={summary['failed_requests']} "
        f"duration={summary['total_duration']:.2f}s"
    )
    for i, item in enumerate(results["results"], 1):
        label = "OK" if item["success"] else "ERR"
        if item["success"] and item.get("content"):
            raw = str(item["content"])
            preview = raw[:100].replace("\n", " ") + ("..." if len(raw) > 100 else "")
        elif item["success"] and item.get("content_base64"):
            preview = f"[Binary content, {item['size']} bytes]"
        else:
            preview = str(item.get("error", "Unknown error"))
        print(f"   {i}. [{label}] {item['url']} ({item['format']}): {preview}")


async def main() -> None:
    """Submit a sample batch job, poll progress, and save results."""
    urls_to_process = [
        {"url": "https://koomen.dev/essays/horseless-carriages/", "format": "text"},
        {"url": "https://koomen.dev/essays/horseless-carriages/", "format": "markdown"},
        {"url": "https://httpbin.org/json", "format": "json"},
        {"url": "https://example.com", "format": "html"},
    ]

    job_id = await submit_batch_job(urls_to_process)
    if not job_id:
        return

    final_status = await wait_for_completion(job_id)
    if not final_status or final_status["status"] != "completed":
        print("Job did not complete successfully.")
        return

    results = await download_job_results(job_id)
    if not results:
        return

    _print_results(results)
    filename = f"batch_results_{job_id[:8]}.json"
    with open(filename, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2)
    print(f"Saved full results to: {filename}")


if __name__ == "__main__":
    asyncio.run(main())

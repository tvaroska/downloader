#!/usr/bin/env python3
"""
Example: Using the new job-based batch processing API.

This example demonstrates:
1. Submitting a batch job
2. Polling for job status
3. Downloading results when ready
"""

import asyncio
import json
import time
from typing import Any

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
POLL_INTERVAL = 2  # seconds


async def submit_batch_job(urls_with_formats: list[dict]) -> str:
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
            print(f"❌ Failed to submit job: {response.status_code}")
            print(response.text)
            return None

        result = response.json()
        job_id = result["job_id"]

        print("✅ Job submitted successfully!")
        print(f"   Job ID: {job_id}")
        print(f"   Status: {result['status']}")
        print(f"   Total URLs: {result['total_urls']}")
        if result.get("estimated_completion"):
            print(f"   Estimated completion: {result['estimated_completion']}")

        return job_id


async def get_job_status(job_id: str) -> dict[str, Any]:
    """Get the current status of a job."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/jobs/{job_id}/status")

        if response.status_code != 200:
            print(f"❌ Failed to get job status: {response.status_code}")
            return None

        return response.json()


async def download_job_results(job_id: str) -> dict[str, Any]:
    """Download the results of a completed job."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/jobs/{job_id}/results")

        if response.status_code != 200:
            print(f"❌ Failed to download results: {response.status_code}")
            print(response.text)
            return None

        return response.json()


async def wait_for_completion(job_id: str) -> dict[str, Any]:
    """Poll job status until completion."""
    print(f"\n🔄 Polling job status every {POLL_INTERVAL} seconds...")

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

        if status == "completed":
            print(f"✅ Job completed successfully in {elapsed:.1f} seconds!")
            return status_info
        elif status == "failed":
            print(f"❌ Job failed: {status_info.get('error_message', 'Unknown error')}")
            return status_info
        elif status == "cancelled":
            print("⚠️  Job was cancelled")
            return status_info

        await asyncio.sleep(POLL_INTERVAL)


async def main():
    """Main example function."""
    print("🚀 REST API Downloader - Batch Job Example")
    print("=" * 50)

    # Define URLs to process with different formats
    urls_to_process = [
        {
            "url": "https://koomen.dev/essays/horseless-carriages/",
            "format": "text",
        },
        {
            "url": "https://koomen.dev/essays/horseless-carriages/",
            "format": "markdown",
        },
        {"url": "https://httpbin.org/json", "format": "json"},
        {"url": "https://example.com", "format": "html"},
    ]

    print(f"📋 Processing {len(urls_to_process)} URLs:")
    for i, url_info in enumerate(urls_to_process, 1):
        print(f"   {i}. {url_info['url']} (format: {url_info['format']})")

    # Step 1: Submit the batch job
    print("\n📤 Submitting batch job...")
    job_id = await submit_batch_job(urls_to_process)

    if not job_id:
        print("❌ Failed to submit job. Exiting.")
        return

    # Step 2: Wait for completion
    final_status = await wait_for_completion(job_id)

    if not final_status or final_status["status"] != "completed":
        print("❌ Job did not complete successfully. Exiting.")
        return

    # Step 3: Download results
    print("\n📥 Downloading results...")
    results = await download_job_results(job_id)

    if not results:
        print("❌ Failed to download results. Exiting.")
        return

    # Step 4: Display summary
    print("\n📊 Results Summary:")
    summary = results["summary"]
    print(f"   Total requests: {summary['total_requests']}")
    print(f"   Successful: {summary['successful_requests']}")
    print(f"   Failed: {summary['failed_requests']}")
    print(f"   Success rate: {summary['success_rate']:.1f}%")
    print(f"   Total duration: {summary['total_duration']:.2f}s")

    # Step 5: Show individual results
    print("\n📄 Individual Results:")
    for i, result in enumerate(results["results"], 1):
        status_icon = "✅" if result["success"] else "❌"
        content_preview = ""

        if result["success"]:
            if result.get("content"):
                content_preview = result["content"][:100].replace("\n", " ")
                if len(result["content"]) > 100:
                    content_preview += "..."
            elif result.get("content_base64"):
                content_preview = f"[Binary content, {result['size']} bytes]"
        else:
            content_preview = result.get("error", "Unknown error")

        print(f"   {i}. {status_icon} {result['url']}")
        print(f"      Format: {result['format']}, Duration: {result.get('duration', 0):.2f}s")
        print(f"      Content: {content_preview}")
        print()

    # Optionally save results to file
    filename = f"batch_results_{job_id[:8]}.json"
    with open(filename, "w") as f:
        json.dump(results, f, indent=2)
    print(f"💾 Full results saved to: {filename}")


if __name__ == "__main__":
    asyncio.run(main())

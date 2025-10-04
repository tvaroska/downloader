#!/usr/bin/env python3
"""
Batch Processing Example

This example demonstrates how to use the batch processing endpoint of the
REST API Downloader service to efficiently download and process multiple URLs
with different formats concurrently.

Usage:
    python examples/batch_processing.py

Requirements:
    - REST API Downloader server running (e.g., in Docker container)
    - httpx package installed

Author: REST API Downloader Examples
"""

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("batch_outputs")

# Example URLs for batch processing
EXAMPLE_URLS = [
    {"url": "https://example.com", "format": "text"},
    {"url": "https://httpbin.org/html", "format": "markdown"},
    {"url": "https://github.com/python/cpython", "format": "text"},
    {
        "url": "https://docs.python.org/3/library/asyncio.html",
        "format": "markdown",
    },
    {"url": "https://fastapi.tiangolo.com/", "format": "html"},
]

# PDF examples (separate batch to demonstrate PDF functionality)
PDF_URLS = [
    {"url": "https://example.com", "format": "pdf"},
    {"url": "https://httpbin.org/html", "format": "pdf"},
]


async def check_server_health() -> bool:
    """Check if the server is running and healthy."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code == 200:
                health_data = response.json()
                print(f"✅ Server is healthy: {health_data}")
                return True
            else:
                print(f"❌ Server health check failed: {response.status_code}")
                return False
    except Exception as e:
        print(f"❌ Cannot connect to server: {e}")
        print(f"   Make sure the server is running at {BASE_URL}")
        return False


async def process_batch_request(urls: list[dict], batch_name: str, **kwargs) -> dict[str, Any]:
    """
    Process a batch request and return the results.

    Args:
        urls: List of URL configurations
        batch_name: Name for this batch (for logging)
        **kwargs: Additional batch request parameters

    Returns:
        Batch response data
    """
    print(f"\n🚀 Starting batch '{batch_name}' with {len(urls)} URLs")

    # Default batch request configuration
    batch_request = {
        "urls": urls,
        "default_format": "text",
        "concurrency_limit": 5,
        "timeout_per_url": 30,
        **kwargs,  # Allow overriding defaults
    }

    print("📊 Batch configuration:")
    print(f"   Default format: {batch_request['default_format']}")
    print(f"   Concurrency limit: {batch_request['concurrency_limit']}")
    print(f"   Timeout per URL: {batch_request['timeout_per_url']}s")

    start_time = time.time()

    try:
        async with httpx.AsyncClient(timeout=300) as client:  # 5 minute timeout
            response = await client.post(f"{BASE_URL}/batch", json=batch_request)

            if response.status_code == 200:
                batch_data = response.json()
                duration = time.time() - start_time

                print(f"✅ Batch '{batch_name}' completed in {duration:.2f}s")
                print(f"   Total requests: {batch_data['total_requests']}")
                print(f"   Successful: {batch_data['successful_requests']}")
                print(f"   Failed: {batch_data['failed_requests']}")
                print(f"   Success rate: {batch_data['success_rate']:.1f}%")
                print(f"   Batch ID: {batch_data['batch_id']}")

                return batch_data
            else:
                print(f"❌ Batch '{batch_name}' failed: {response.status_code}")
                print(f"   Error: {response.text}")
                return {
                    "error": response.text,
                    "status_code": response.status_code,
                }

    except asyncio.TimeoutError:
        print(f"⏰ Batch '{batch_name}' timed out")
        return {"error": "Request timeout"}
    except Exception as e:
        print(f"💥 Batch '{batch_name}' error: {e}")
        return {"error": str(e)}


def save_batch_results(batch_data: dict[str, Any], batch_name: str):
    """Save batch results to files."""
    if "error" in batch_data:
        print(f"⚠️  Skipping save for failed batch '{batch_name}'")
        return

    OUTPUT_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # Save detailed JSON results
    json_file = OUTPUT_DIR / f"{batch_name}_{timestamp}.json"
    with open(json_file, "w") as f:
        json.dump(batch_data, f, indent=2)
    print(f"💾 Saved detailed results to: {json_file}")

    # Save individual content files
    for i, result in enumerate(batch_data["results"]):
        if result["success"]:
            # Determine file extension based on format
            format_map = {
                "text": "txt",
                "markdown": "md",
                "html": "html",
                "json": "json",
                "pdf": "pdf",
            }
            ext = format_map.get(result["format"], "txt")

            # Create safe filename
            url_part = result["url"].replace("https://", "").replace("http://", "")
            url_part = "".join(c for c in url_part if c.isalnum() or c in "._-")[:50]
            filename = f"{batch_name}_{i + 1:02d}_{url_part}.{ext}"

            content_file = OUTPUT_DIR / filename

            if result["format"] == "pdf":
                # Save PDF as binary
                import base64

                pdf_content = base64.b64decode(result["content_base64"])
                with open(content_file, "wb") as f:
                    f.write(pdf_content)
            else:
                # Save text content
                with open(content_file, "w", encoding="utf-8") as f:
                    f.write(result["content"])

            print(f"📄 Saved content to: {content_file}")


def print_batch_summary(batch_data: dict[str, Any], batch_name: str):
    """Print a detailed summary of batch results."""
    if "error" in batch_data:
        print(f"\n❌ Batch '{batch_name}' Summary: FAILED")
        print(f"   Error: {batch_data['error']}")
        return

    print(f"\n📊 Batch '{batch_name}' Summary:")
    print(f"   Batch ID: {batch_data['batch_id']}")
    print(f"   Overall Success: {'✅ Yes' if batch_data['success'] else '❌ No'}")
    print(f"   Total Duration: {batch_data['total_duration']:.2f}s")
    print(f"   Total Requests: {batch_data['total_requests']}")
    print(f"   Successful: {batch_data['successful_requests']}")
    print(f"   Failed: {batch_data['failed_requests']}")
    print(f"   Success Rate: {batch_data['success_rate']:.1f}%")

    print("\n📋 Individual Results:")
    for i, result in enumerate(batch_data["results"], 1):
        status = "✅" if result["success"] else "❌"
        duration = f"{result['duration']:.2f}s" if result.get("duration") else "N/A"
        size = f"{result['size']:,}B" if result.get("size") else "N/A"

        print(f"   {status} [{i:02d}] {result['url']}")
        print(f"       Format: {result['format']} | Duration: {duration} | Size: {size}")

        if not result["success"]:
            print(f"       Error: {result.get('error', 'Unknown error')}")

        if result.get("content") and len(result["content"]) < 100:
            preview = (
                result["content"][:97] + "..." if len(result["content"]) > 97 else result["content"]
            )
            print(f"       Preview: {preview}")


async def demonstrate_basic_batch():
    """Demonstrate basic batch processing with different formats."""
    print("\n" + "=" * 80)
    print("🎯 BASIC BATCH PROCESSING DEMONSTRATION")
    print("=" * 80)

    batch_data = await process_batch_request(
        urls=EXAMPLE_URLS,
        batch_name="basic_demo",
        default_format="text",
        concurrency_limit=3,
    )

    print_batch_summary(batch_data, "basic_demo")
    save_batch_results(batch_data, "basic_demo")


async def demonstrate_pdf_batch():
    """Demonstrate PDF batch processing."""
    print("\n" + "=" * 80)
    print("📄 PDF BATCH PROCESSING DEMONSTRATION")
    print("=" * 80)

    batch_data = await process_batch_request(
        urls=PDF_URLS,
        batch_name="pdf_demo",
        default_format="pdf",
        concurrency_limit=2,  # Lower concurrency for PDF generation
        timeout_per_url=60,  # Longer timeout for PDF generation
    )

    print_batch_summary(batch_data, "pdf_demo")
    save_batch_results(batch_data, "pdf_demo")


async def demonstrate_error_handling():
    """Demonstrate error handling in batch processing."""
    print("\n" + "=" * 80)
    print("⚠️  ERROR HANDLING DEMONSTRATION")
    print("=" * 80)

    # Mix of valid and invalid URLs
    error_urls = [
        {"url": "https://httpbin.org/status/200", "format": "text"},
        {"url": "https://httpbin.org/status/404", "format": "text"},
        {"url": "https://httpbin.org/status/500", "format": "text"},
        {"url": "invalid-url-format", "format": "text"},
        {
            "url": "https://httpbin.org/delay/10",
            "format": "text",
        },  # Will timeout
    ]

    batch_data = await process_batch_request(
        urls=error_urls,
        batch_name="error_demo",
        default_format="text",
        concurrency_limit=5,
        timeout_per_url=5,  # Short timeout to demonstrate timeout handling
    )

    print_batch_summary(batch_data, "error_demo")
    save_batch_results(batch_data, "error_demo")


async def demonstrate_high_concurrency():
    """Demonstrate high concurrency batch processing."""
    print("\n" + "=" * 80)
    print("🚀 HIGH CONCURRENCY DEMONSTRATION")
    print("=" * 80)

    # Create many URLs for concurrent processing
    high_concurrency_urls = [
        {"url": "https://httpbin.org/json", "format": "json"},
        {"url": "https://httpbin.org/html", "format": "text"},
        {"url": "https://httpbin.org/xml", "format": "text"},
    ] * 5  # 15 URLs total

    batch_data = await process_batch_request(
        urls=high_concurrency_urls,
        batch_name="concurrency_demo",
        default_format="text",
        concurrency_limit=10,  # High concurrency
    )

    print_batch_summary(batch_data, "concurrency_demo")
    save_batch_results(batch_data, "concurrency_demo")


async def main():
    """Main entry point for batch processing examples."""
    print("🎬 REST API Downloader - Batch Processing Examples")
    print("=" * 60)

    # Check server health first
    if not await check_server_health():
        print("\n❌ Cannot proceed with examples - server is not available")
        print("\n🔧 To start the server:")
        print("   docker build -t downloader .")
        print("   docker run -p 8000:80 downloader")
        return

    try:
        # Run various demonstrations
        await demonstrate_basic_batch()
        await demonstrate_pdf_batch()
        await demonstrate_error_handling()
        await demonstrate_high_concurrency()

        print("\n🎯 Examples Summary:")
        print("   All batch processing examples completed!")
        print(f"   Check the '{OUTPUT_DIR}' directory for saved results.")
        print("   Each batch creates both detailed JSON logs and individual " "content files.")

        print("\n💡 Tips for using batch processing:")
        print("   • Use appropriate concurrency limits based on your server resources")
        print("   • Set reasonable timeouts for different content types " "(PDF takes longer)")
        print("   • Monitor the batch_id for tracking specific batch operations")
        print(
            "   • Handle partial failures gracefully - some URLs may fail " "while others succeed"
        )
        print(
            "   • Use different formats in the same batch for efficient " "multi-format processing"
        )

        print("\n🏁 Examples completed successfully!")

    except KeyboardInterrupt:
        print("\n⏹️  Examples interrupted by user")
    except Exception as e:
        print(f"\n💥 Examples failed with error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

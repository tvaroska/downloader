#!/usr/bin/env python3
"""
Concurrent PDF Generation Example

This example demonstrates how to make multiple concurrent PDF generation requests
to a running REST API Downloader service. It tests the service's ability to handle
concurrent PDF generation load and measures performance characteristics.

Usage:
    python examples/concurrent_pdf_requests.py

Requirements:
    - REST API Downloader server running (e.g., in Docker container)
    - httpx, asyncio, aiofiles packages installed

Author: REST API Downloader Examples
"""

import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("pdf_outputs")
CONCURRENT_REQUESTS = 10
REQUEST_TIMEOUT = 60  # seconds
SAVE_PDFS = True  # Set to False to skip saving PDFs to disk

# Test URLs - mix of different complexity levels
TEST_URLS = [
    "https://example.com",
    "https://httpbin.org/html",
    "https://github.com/python/cpython",
    "https://docs.python.org/3/",
    "https://fastapi.tiangolo.com/",
    "https://www.wikipedia.org/",
    "https://news.ycombinator.com",
    "https://stackoverflow.com/questions",
    "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    "https://www.w3.org/TR/html52/",
]


class PDFRequestResult:
    """Container for PDF request results and metrics."""

    def __init__(self, url: str, start_time: float):
        self.url = url
        self.start_time = start_time
        self.end_time: float = 0
        self.duration: float = 0
        self.success: bool = False
        self.status_code: int = 0
        self.error: str = ""
        self.pdf_size: int = 0
        self.file_path: str = ""

    def finish(self, success: bool, status_code: int = 0, error: str = "",
               pdf_size: int = 0, file_path: str = ""):
        """Mark the request as finished and calculate metrics."""
        self.end_time = time.time()
        self.duration = self.end_time - self.start_time
        self.success = success
        self.status_code = status_code
        self.error = error
        self.pdf_size = pdf_size
        self.file_path = file_path

    def to_dict(self) -> dict[str, Any]:
        """Convert result to dictionary for JSON serialization."""
        return {
            "url": self.url,
            "success": self.success,
            "duration": round(self.duration, 2),
            "status_code": self.status_code,
            "error": self.error,
            "pdf_size": self.pdf_size,
            "file_path": self.file_path,
            "timestamp": datetime.fromtimestamp(self.start_time).isoformat()
        }


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


async def generate_pdf_request(
    client: httpx.AsyncClient, url: str, request_id: int
) -> PDFRequestResult:
    """
    Make a single PDF generation request.

    Args:
        client: HTTP client instance
        url: URL to convert to PDF
        request_id: Unique identifier for this request

    Returns:
        PDFRequestResult with timing and outcome data
    """
    result = PDFRequestResult(url, time.time())

    try:
        print(f"🚀 [{request_id:02d}] Starting PDF generation for: {url}")

        # Make the PDF request
        response = await client.get(
            f"{BASE_URL}/{url}",
            headers={"Accept": "application/pdf"},
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 200:
            pdf_content = response.content
            pdf_size = len(pdf_content)

            file_path = ""
            if SAVE_PDFS:
                # Save PDF to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                filename = f"request_{request_id:02d}_{url.replace('https://', '').replace('/', '_')}.pdf"
                file_path = OUTPUT_DIR / filename

                with open(file_path, "wb") as f:
                    f.write(pdf_content)

                file_path = str(file_path)

            result.finish(
                success=True,
                status_code=200,
                pdf_size=pdf_size,
                file_path=file_path
            )
            print(f"✅ [{request_id:02d}] PDF generated successfully: {pdf_size:,} bytes in {result.duration:.2f}s")

        else:
            # Handle HTTP errors
            try:
                error_data = response.json()
                error_msg = error_data.get("detail", {}).get("error", f"HTTP {response.status_code}")
            except:
                error_msg = f"HTTP {response.status_code}: {response.text[:100]}"

            result.finish(
                success=False,
                status_code=response.status_code,
                error=error_msg
            )
            print(f"❌ [{request_id:02d}] PDF generation failed: {error_msg}")

    except asyncio.TimeoutError:
        result.finish(success=False, error="Request timeout")
        print(f"⏰ [{request_id:02d}] PDF generation timed out after {REQUEST_TIMEOUT}s")

    except Exception as e:
        result.finish(success=False, error=str(e))
        print(f"💥 [{request_id:02d}] PDF generation error: {e}")

    return result


async def run_concurrent_pdf_test() -> list[PDFRequestResult]:
    """
    Run concurrent PDF generation requests and collect results.
    
    Returns:
        List of PDFRequestResult objects with timing and outcome data
    """
    print(f"🎯 Starting concurrent PDF test with {CONCURRENT_REQUESTS} requests")
    print(f"📊 Target URLs: {len(TEST_URLS)} different websites")
    print(f"⏱️  Timeout: {REQUEST_TIMEOUT} seconds per request")
    print(f"💾 Save PDFs: {'Yes' if SAVE_PDFS else 'No'}")
    print()

    # Create HTTP client with connection pooling
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    timeout = httpx.Timeout(REQUEST_TIMEOUT)

    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        # Create tasks for concurrent execution
        tasks = []
        for i in range(CONCURRENT_REQUESTS):
            # Cycle through test URLs if we have more requests than URLs
            url = TEST_URLS[i % len(TEST_URLS)]
            task = generate_pdf_request(client, url, i + 1)
            tasks.append(task)

        # Start timer and run all requests concurrently
        start_time = time.time()
        print(f"🏁 Starting {len(tasks)} concurrent requests at {datetime.now().strftime('%H:%M:%S')}")

        # Execute all requests concurrently
        results = await asyncio.gather(*tasks, return_exceptions=False)

        total_time = time.time() - start_time
        print(f"🏆 All requests completed in {total_time:.2f} seconds")

    return results


def analyze_results(results: list[PDFRequestResult]) -> dict[str, Any]:
    """
    Analyze the test results and generate statistics.

    Args:
        results: List of PDFRequestResult objects

    Returns:
        Dictionary with analysis results
    """
    successful_results = [r for r in results if r.success]
    failed_results = [r for r in results if not r.success]

    analysis = {
        "total_requests": len(results),
        "successful_requests": len(successful_results),
        "failed_requests": len(failed_results),
        "success_rate": len(successful_results) / len(results) * 100,
    }

    if successful_results:
        durations = [r.duration for r in successful_results]
        pdf_sizes = [r.pdf_size for r in successful_results]

        analysis.update({
            "timing_stats": {
                "min_duration": min(durations),
                "max_duration": max(durations),
                "avg_duration": statistics.mean(durations),
                "median_duration": statistics.median(durations),
                "total_duration": sum(durations)
            },
            "size_stats": {
                "min_pdf_size": min(pdf_sizes),
                "max_pdf_size": max(pdf_sizes),
                "avg_pdf_size": statistics.mean(pdf_sizes),
                "total_pdf_size": sum(pdf_sizes)
            }
        })

        # Calculate requests per second
        if durations:
            analysis["requests_per_second"] = len(successful_results) / max(durations)

    # Error analysis
    if failed_results:
        error_types = {}
        for result in failed_results:
            error_key = f"{result.status_code}: {result.error}"
            error_types[error_key] = error_types.get(error_key, 0) + 1
        analysis["error_breakdown"] = error_types

    return analysis


def print_results_summary(results: list[PDFRequestResult], analysis: dict[str, Any]):
    """Print a formatted summary of the test results."""
    print("\n" + "="*80)
    print("📊 CONCURRENT PDF GENERATION TEST RESULTS")
    print("="*80)

    # Overview
    print("📈 Overview:")
    print(f"   Total Requests: {analysis['total_requests']}")
    print(f"   Successful: {analysis['successful_requests']} ({analysis['success_rate']:.1f}%)")
    print(f"   Failed: {analysis['failed_requests']}")

    # Timing statistics
    if "timing_stats" in analysis:
        timing = analysis["timing_stats"]
        print("\n⏱️  Timing Statistics:")
        print(f"   Fastest: {timing['min_duration']:.2f}s")
        print(f"   Slowest: {timing['max_duration']:.2f}s")
        print(f"   Average: {timing['avg_duration']:.2f}s")
        print(f"   Median: {timing['median_duration']:.2f}s")
        print(f"   Requests/sec: {analysis.get('requests_per_second', 0):.2f}")

    # Size statistics
    if "size_stats" in analysis:
        sizes = analysis["size_stats"]
        print("\n📦 PDF Size Statistics:")
        print(f"   Smallest: {sizes['min_pdf_size']:,} bytes")
        print(f"   Largest: {sizes['max_pdf_size']:,} bytes")
        print(f"   Average: {sizes['avg_pdf_size']:,.0f} bytes")
        print(f"   Total: {sizes['total_pdf_size']:,} bytes")

    # Error breakdown
    if "error_breakdown" in analysis:
        print("\n❌ Error Breakdown:")
        for error, count in analysis["error_breakdown"].items():
            print(f"   {error}: {count} occurrences")

    # Individual results
    print("\n📋 Individual Results:")
    for i, result in enumerate(results, 1):
        status = "✅" if result.success else "❌"
        duration_str = f"{result.duration:.2f}s" if result.duration > 0 else "N/A"
        size_str = f"{result.pdf_size:,}B" if result.pdf_size > 0 else "N/A"

        print(f"   {status} [{i:02d}] {result.url} - {duration_str} - {size_str}")
        if not result.success and result.error:
            print(f"       Error: {result.error}")


async def save_detailed_results(results: list[PDFRequestResult], analysis: dict[str, Any]):
    """Save detailed results to JSON file for further analysis."""
    output_data = {
        "test_config": {
            "base_url": BASE_URL,
            "concurrent_requests": CONCURRENT_REQUESTS,
            "request_timeout": REQUEST_TIMEOUT,
            "test_urls": TEST_URLS,
            "save_pdfs": SAVE_PDFS
        },
        "test_timestamp": datetime.now().isoformat(),
        "analysis": analysis,
        "individual_results": [result.to_dict() for result in results]
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    results_file = OUTPUT_DIR / f"concurrent_pdf_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"

    with open(results_file, "w") as f:
        json.dump(output_data, f, indent=2)

    print(f"\n💾 Detailed results saved to: {results_file}")


async def main():
    """Main entry point for the concurrent PDF test."""
    print("🎬 REST API Downloader - Concurrent PDF Generation Test")
    print("=" * 60)

    # Check server health first
    if not await check_server_health():
        print("\n❌ Cannot proceed with test - server is not available")
        print("\n🔧 To start the server:")
        print("   docker build -t downloader .")
        print("   docker run -p 8000:80 downloader")
        return

    print()

    try:
        # Run the concurrent test
        results = await run_concurrent_pdf_test()

        # Analyze results
        analysis = analyze_results(results)

        # Print summary
        print_results_summary(results, analysis)

        # Save detailed results
        await save_detailed_results(results, analysis)

        # Final recommendations
        print("\n🎯 Test Recommendations:")
        if analysis["success_rate"] >= 90:
            print("   ✅ Excellent: PDF generation is working reliably")
        elif analysis["success_rate"] >= 70:
            print("   ⚠️  Good: Some failures detected, investigate error patterns")
        else:
            print("   ❌ Poor: High failure rate, check server configuration and logs")

        if "timing_stats" in analysis:
            avg_time = analysis["timing_stats"]["avg_duration"]
            if avg_time <= 10:
                print("   ✅ Excellent: Fast PDF generation times")
            elif avg_time <= 30:
                print("   ⚠️  Good: Reasonable PDF generation times")
            else:
                print("   ❌ Slow: PDF generation is taking too long")

        print("\n🏁 Test completed successfully!")

    except KeyboardInterrupt:
        print("\n⏹️  Test interrupted by user")
    except Exception as e:
        print(f"\n💥 Test failed with error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

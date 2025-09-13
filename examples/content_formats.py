#!/usr/bin/env python3
"""
Content Formats Comparison Example

This example demonstrates all supported content formats by requesting the same URL
in different formats and comparing the results. Useful for understanding the
differences between text, markdown, HTML, JSON, and PDF outputs.

Usage:
    python examples/content_formats.py

Requirements:
    - REST API Downloader server running
    - httpx package installed

Author: REST API Downloader Examples
"""

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("format_comparison")
TIMEOUT = 45  # seconds

# Test URL - choose one with rich content for good comparison
TEST_URL = "https://docs.python.org/3/tutorial/introduction.html"

# All supported content formats
CONTENT_FORMATS = {
    "text": {
        "accept_header": "text/plain",
        "description": "Plain text with article extraction",
        "file_extension": "txt"
    },
    "markdown": {
        "accept_header": "text/markdown",
        "description": "Markdown with structured content",
        "file_extension": "md"
    },
    "html": {
        "accept_header": "text/html",
        "description": "Original HTML content",
        "file_extension": "html"
    },
    "json": {
        "accept_header": "application/json",
        "description": "JSON with base64 content and metadata",
        "file_extension": "json"
    },
    "pdf": {
        "accept_header": "application/pdf",
        "description": "PDF generated via Playwright",
        "file_extension": "pdf"
    },
    "default": {
        "accept_header": None,
        "description": "Default format (should be text/plain)",
        "file_extension": "txt"
    }
}


class FormatResult:
    """Container for content format request results."""

    def __init__(self, format_name: str, accept_header: str):
        self.format_name = format_name
        self.accept_header = accept_header
        self.success = False
        self.status_code = 0
        self.duration = 0.0
        self.content_size = 0
        self.response_content_type = ""
        self.error = ""
        self.file_path = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        return {
            "format_name": self.format_name,
            "accept_header": self.accept_header,
            "success": self.success,
            "status_code": self.status_code,
            "duration": round(self.duration, 2),
            "content_size": self.content_size,
            "response_content_type": self.response_content_type,
            "error": self.error,
            "file_path": self.file_path
        }


async def request_format(client: httpx.AsyncClient, format_name: str, format_config: dict[str, str]) -> FormatResult:
    """
    Request content in a specific format and measure performance.
    
    Args:
        client: HTTP client instance
        format_name: Name of the format (text, markdown, etc.)
        format_config: Configuration for this format
        
    Returns:
        FormatResult with timing and outcome data
    """
    result = FormatResult(format_name, format_config.get("accept_header"))

    print(f"🔄 Requesting {format_name} format...")

    try:
        start_time = time.time()

        # Prepare headers
        headers = {}
        if format_config["accept_header"]:
            headers["Accept"] = format_config["accept_header"]

        # Make the request
        response = await client.get(
            f"{BASE_URL}/{TEST_URL}",
            headers=headers,
            timeout=TIMEOUT
        )

        result.duration = time.time() - start_time
        result.status_code = response.status_code
        result.response_content_type = response.headers.get("content-type", "")

        if response.status_code == 200:
            result.success = True

            # Handle different response types
            if format_name == "json":
                # JSON response - save both JSON and decoded content
                json_data = response.json()
                result.content_size = len(response.content)

                # Save JSON
                OUTPUT_DIR.mkdir(exist_ok=True)
                json_file = OUTPUT_DIR / f"{format_name}.{format_config['file_extension']}"
                with open(json_file, "w") as f:
                    json.dump(json_data, f, indent=2)
                result.file_path = str(json_file)

                # Also save decoded content
                if "content" in json_data:
                    decoded_content = base64.b64decode(json_data["content"]).decode('utf-8', errors='ignore')
                    decoded_file = OUTPUT_DIR / f"{format_name}_decoded.txt"
                    with open(decoded_file, "w", encoding="utf-8") as f:
                        f.write(decoded_content)

            elif format_name == "pdf":
                # Binary PDF content
                pdf_content = response.content
                result.content_size = len(pdf_content)

                OUTPUT_DIR.mkdir(exist_ok=True)
                pdf_file = OUTPUT_DIR / f"{format_name}.{format_config['file_extension']}"
                with open(pdf_file, "wb") as f:
                    f.write(pdf_content)
                result.file_path = str(pdf_file)

            else:
                # Text-based content (text, markdown, html)
                text_content = response.text
                result.content_size = len(text_content)

                OUTPUT_DIR.mkdir(exist_ok=True)
                text_file = OUTPUT_DIR / f"{format_name}.{format_config['file_extension']}"
                with open(text_file, "w", encoding="utf-8") as f:
                    f.write(text_content)
                result.file_path = str(text_file)

            print(f"   ✅ Success: {result.content_size:,} bytes in {result.duration:.2f}s")

        else:
            # Handle errors
            result.success = False
            try:
                error_data = response.json()
                result.error = error_data.get("detail", {}).get("error", f"HTTP {response.status_code}")
            except:
                result.error = f"HTTP {response.status_code}: {response.text[:100]}"

            print(f"   ❌ Failed: {result.error}")

    except asyncio.TimeoutError:
        result.error = f"Request timeout after {TIMEOUT}s"
        print(f"   ⏰ Timeout: {result.error}")

    except Exception as e:
        result.error = str(e)
        print(f"   💥 Error: {result.error}")

    return result


async def compare_all_formats() -> dict[str, FormatResult]:
    """
    Request the same URL in all supported formats and compare results.
    
    Returns:
        Dictionary mapping format names to FormatResult objects
    """
    print(f"🎯 Comparing all content formats for: {TEST_URL}")
    print(f"📂 Output directory: {OUTPUT_DIR}")
    print()

    results = {}

    # Create HTTP client
    async with httpx.AsyncClient() as client:
        for format_name, format_config in CONTENT_FORMATS.items():
            result = await request_format(client, format_name, format_config)
            results[format_name] = result

            # Small delay between requests to be nice to the server
            await asyncio.sleep(1)

    return results


def analyze_format_comparison(results: dict[str, FormatResult]) -> dict[str, Any]:
    """
    Analyze the format comparison results.
    
    Args:
        results: Dictionary of format results
        
    Returns:
        Analysis summary
    """
    successful_results = {k: v for k, v in results.items() if v.success}
    failed_results = {k: v for k, v in results.items() if not v.success}

    analysis = {
        "total_formats": len(results),
        "successful_formats": len(successful_results),
        "failed_formats": len(failed_results),
        "success_rate": len(successful_results) / len(results) * 100,
    }

    if successful_results:
        # Size comparison
        sizes = {k: v.content_size for k, v in successful_results.items()}
        analysis["size_comparison"] = {
            "largest": max(sizes.items(), key=lambda x: x[1]),
            "smallest": min(sizes.items(), key=lambda x: x[1]),
            "size_details": sizes
        }

        # Speed comparison
        durations = {k: v.duration for k, v in successful_results.items()}
        analysis["speed_comparison"] = {
            "fastest": min(durations.items(), key=lambda x: x[1]),
            "slowest": max(durations.items(), key=lambda x: x[1]),
            "duration_details": durations
        }

        # Content type mapping
        content_types = {k: v.response_content_type for k, v in successful_results.items()}
        analysis["content_types"] = content_types

    if failed_results:
        analysis["failures"] = {k: v.error for k, v in failed_results.items()}

    return analysis


def print_comparison_results(results: dict[str, FormatResult], analysis: dict[str, Any]):
    """Print a formatted comparison of all format results."""
    print("\n" + "="*80)
    print("📊 CONTENT FORMAT COMPARISON RESULTS")
    print("="*80)

    # Overview
    print("📈 Overview:")
    print(f"   Total formats tested: {analysis['total_formats']}")
    print(f"   Successful: {analysis['successful_formats']} ({analysis['success_rate']:.1f}%)")
    print(f"   Failed: {analysis['failed_formats']}")
    print(f"   Test URL: {TEST_URL}")

    # Detailed results table
    print("\n📋 Detailed Results:")
    print(f"{'Format':<12} {'Status':<8} {'Size':<12} {'Time':<8} {'Content-Type':<30}")
    print("-" * 80)

    for format_name, result in results.items():
        status = "✅ OK" if result.success else "❌ FAIL"
        size_str = f"{result.content_size:,}B" if result.success else "N/A"
        time_str = f"{result.duration:.2f}s" if result.duration > 0 else "N/A"
        content_type = result.response_content_type[:28] + ".." if len(result.response_content_type) > 30 else result.response_content_type

        print(f"{format_name:<12} {status:<8} {size_str:<12} {time_str:<8} {content_type:<30}")

        if not result.success:
            print(f"{'':12} Error: {result.error}")

    # Size comparison
    if "size_comparison" in analysis:
        size_comp = analysis["size_comparison"]
        print("\n📦 Size Comparison:")
        print(f"   Largest: {size_comp['largest'][0]} ({size_comp['largest'][1]:,} bytes)")
        print(f"   Smallest: {size_comp['smallest'][0]} ({size_comp['smallest'][1]:,} bytes)")

        # Size ratio analysis
        largest_size = size_comp['largest'][1]
        smallest_size = size_comp['smallest'][1]
        ratio = largest_size / smallest_size if smallest_size > 0 else 0
        print(f"   Size ratio: {ratio:.1f}x difference")

    # Speed comparison
    if "speed_comparison" in analysis:
        speed_comp = analysis["speed_comparison"]
        print("\n⏱️  Speed Comparison:")
        print(f"   Fastest: {speed_comp['fastest'][0]} ({speed_comp['fastest'][1]:.2f}s)")
        print(f"   Slowest: {speed_comp['slowest'][0]} ({speed_comp['slowest'][1]:.2f}s)")

        # Speed ratio analysis
        slowest_time = speed_comp['slowest'][1]
        fastest_time = speed_comp['fastest'][1]
        time_ratio = slowest_time / fastest_time if fastest_time > 0 else 0
        print(f"   Speed ratio: {time_ratio:.1f}x difference")

    # Format-specific insights
    print("\n💡 Format-Specific Insights:")

    for format_name, format_config in CONTENT_FORMATS.items():
        if format_name in results and results[format_name].success:
            result = results[format_name]
            print(f"   📄 {format_name.upper()}: {format_config['description']}")
            print(f"      File: {result.file_path}")

            # Format-specific analysis
            if format_name == "pdf" and result.content_size > 0:
                print(f"      PDF is {result.content_size / 1024:.1f}KB - suitable for document archival")
            elif format_name == "json" and result.content_size > 0:
                print("      JSON includes metadata - ideal for programmatic processing")
            elif format_name == "markdown" and result.content_size > 0:
                print("      Markdown preserves structure - good for documentation")
            elif format_name == "text" and result.content_size > 0:
                print("      Plain text is clean - best for content analysis")

    # Recommendations
    print("\n🎯 Recommendations:")

    if "text" in results and results["text"].success:
        print("   📝 Use text/plain for: Content analysis, search indexing, NLP processing")

    if "markdown" in results and results["markdown"].success:
        print("   📝 Use text/markdown for: Documentation, structured content, CMS integration")

    if "html" in results and results["html"].success:
        print("   🌐 Use text/html for: Web scraping, style preservation, full content")

    if "json" in results and results["json"].success:
        print("   📊 Use application/json for: API integration, metadata analysis, structured processing")

    if "pdf" in results and results["pdf"].success:
        print("   📄 Use application/pdf for: Document archival, printing, visual preservation")

    # Failure analysis
    if analysis["failed_formats"] > 0:
        print("\n❌ Failed Formats:")
        for format_name, error in analysis.get("failures", {}).items():
            print(f"   {format_name}: {error}")


async def save_comparison_results(results: dict[str, FormatResult], analysis: dict[str, Any]):
    """Save the comparison results to a JSON summary file."""
    summary_data = {
        "test_url": TEST_URL,
        "test_timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis": analysis,
        "individual_results": {k: v.to_dict() for k, v in results.items()}
    }

    OUTPUT_DIR.mkdir(exist_ok=True)
    summary_file = OUTPUT_DIR / "format_comparison_summary.json"

    with open(summary_file, "w") as f:
        json.dump(summary_data, f, indent=2)

    print(f"\n💾 Comparison summary saved to: {summary_file}")


async def main():
    """Main entry point for content format comparison."""
    print("🎬 REST API Downloader - Content Format Comparison")
    print("=" * 60)

    # Check server health
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code != 200:
                print(f"❌ Server health check failed: {response.status_code}")
                return
            print("✅ Server is healthy")
    except Exception as e:
        print(f"❌ Cannot connect to server: {e}")
        return

    print()

    try:
        # Run format comparison
        results = await compare_all_formats()

        # Analyze results
        analysis = analyze_format_comparison(results)

        # Print detailed comparison
        print_comparison_results(results, analysis)

        # Save results
        await save_comparison_results(results, analysis)

        print("\n🏁 Format comparison completed!")
        print(f"📁 All output files saved to: {OUTPUT_DIR}/")

    except KeyboardInterrupt:
        print("\n⏹️  Comparison interrupted by user")
    except Exception as e:
        print(f"\n💥 Comparison failed with error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

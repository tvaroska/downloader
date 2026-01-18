#!/usr/bin/env python3
"""
Basic Usage Example

This example demonstrates the fundamental usage patterns of the REST API
Downloader service.
It shows how to make requests for different content formats and handle responses.

Usage:
    python examples/basic_usage.py

Requirements:
    - REST API Downloader server running (e.g., in Docker container)
    - httpx package installed

Author: REST API Downloader Examples
"""

import asyncio
import base64
import json
from pathlib import Path

import httpx

# Configuration
BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("basic_outputs")
TIMEOUT = 30  # seconds

# Test URLs for different scenarios
TEST_URLS = {
    "simple": "https://example.com",
    "news": "https://news.ycombinator.com",
    "documentation": "https://docs.python.org/3/",
    "json_api": "https://httpbin.org/json",
}


async def check_server_health():
    """Check if the server is running and get status info."""
    print("🔍 Checking server health...")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)

            if response.status_code == 200:
                health_data = response.json()
                print("✅ Server is healthy!")
                print(f"   Version: {health_data.get('version', 'unknown')}")
                print(f"   Auth enabled: {health_data.get('auth_enabled', 'unknown')}")
                return True
            else:
                print(f"❌ Server returned status {response.status_code}")
                return False

    except Exception as e:
        print(f"❌ Cannot connect to server: {e}")
        print(f"   Make sure the server is running at {BASE_URL}")
        return False


async def demo_text_format():
    """Demonstrate plain text content extraction."""
    print("\n📄 Demo: Plain Text Format (Article Extraction)")
    print("-" * 50)

    url = TEST_URLS["news"]
    print(f"🌐 Extracting article text from: {url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/plain"},
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                text_content = response.text
                print(f"✅ Success! Extracted {len(text_content)} characters")
                print("📊 Response headers:")
                print(f"   Content-Type: {response.headers.get('content-type')}")
                print(f"   Original URL: {response.headers.get('x-original-url')}")
                print(f"   Content Length: {response.headers.get('x-content-length')}")

                # Show preview
                preview = text_content[:300] + "..." if len(text_content) > 300 else text_content
                print("\n📝 Content preview:")
                print(f"   {preview}")

                # Save to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "article_text.txt"
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(text_content)
                print(f"💾 Saved to: {output_file}")

            else:
                print(f"❌ Request failed with status {response.status_code}")
                print(f"   Error: {response.text}")

        except Exception as e:
            print(f"💥 Error: {e}")


async def demo_markdown_format():
    """Demonstrate markdown content conversion."""
    print("\n📝 Demo: Markdown Format")
    print("-" * 50)

    url = TEST_URLS["documentation"]
    print(f"🌐 Converting to markdown: {url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/markdown"},
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                markdown_content = response.text
                print(f"✅ Success! Generated {len(markdown_content)} characters of markdown")

                # Show preview
                lines = markdown_content.split("\n")
                preview_lines = lines[:10] if len(lines) > 10 else lines
                print("\n📝 Markdown preview:")
                for line in preview_lines:
                    print(f"   {line}")
                if len(lines) > 10:
                    print("   ...")

                # Save to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "content.md"
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(markdown_content)
                print(f"💾 Saved to: {output_file}")

            else:
                print(f"❌ Request failed with status {response.status_code}")

        except Exception as e:
            print(f"💥 Error: {e}")


async def demo_json_format():
    """Demonstrate JSON response with metadata."""
    print("\n📊 Demo: JSON Format with Metadata")
    print("-" * 50)

    url = TEST_URLS["json_api"]
    print(f"🌐 Getting JSON response: {url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                json_data = response.json()
                print("✅ Success! Received structured JSON response")
                print("📊 Response structure:")
                print(f"   Success: {json_data.get('success')}")
                print(f"   URL: {json_data.get('url')}")
                print(f"   Size: {json_data.get('size')} bytes")
                print(f"   Content Type: {json_data.get('content_type')}")

                # Decode base64 content
                if "content" in json_data:
                    content_b64 = json_data["content"]
                    decoded_content = base64.b64decode(content_b64).decode("utf-8", errors="ignore")
                    print(f"   Decoded content length: {len(decoded_content)} characters")

                # Show metadata
                if "metadata" in json_data:
                    metadata = json_data["metadata"]
                    print("📋 Metadata:")
                    print(f"   Status Code: {metadata.get('status_code')}")
                    print(f"   Headers count: {len(metadata.get('headers', {}))}")

                # Save to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "response.json"
                with open(output_file, "w") as f:
                    json.dump(json_data, f, indent=2)
                print(f"💾 Saved to: {output_file}")

            else:
                print(f"❌ Request failed with status {response.status_code}")

        except Exception as e:
            print(f"💥 Error: {e}")


async def demo_pdf_format():
    """Demonstrate PDF generation."""
    print("\n📄 Demo: PDF Generation")
    print("-" * 50)

    url = TEST_URLS["simple"]
    print(f"🌐 Generating PDF from: {url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "application/pdf"},
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                pdf_content = response.content
                print(f"✅ Success! Generated PDF of {len(pdf_content):,} bytes")
                print("📊 Response headers:")
                print(f"   Content-Type: {response.headers.get('content-type')}")
                print(f"   Content-Length: {response.headers.get('content-length')}")
                print(f"   Content-Disposition: {response.headers.get('content-disposition')}")

                # Save to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "generated.pdf"
                with open(output_file, "wb") as f:
                    f.write(pdf_content)
                print(f"💾 Saved to: {output_file}")

            elif response.status_code == 503:
                print("⏳ PDF service temporarily unavailable (503)")
                print(
                    "   This is normal under high load - the service limits "
                    "concurrent PDF generation"
                )

            else:
                print(f"❌ Request failed with status {response.status_code}")
                try:
                    error_data = response.json()
                    print(f"   Error: {error_data}")
                except Exception:
                    print(f"   Raw response: {response.text}")

        except Exception as e:
            print(f"💥 Error: {e}")


async def demo_html_format():
    """Demonstrate HTML content retrieval."""
    print("\n🌐 Demo: HTML Format")
    print("-" * 50)

    url = TEST_URLS["simple"]
    print(f"🌐 Getting HTML content: {url}")

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/html"},
                timeout=TIMEOUT,
            )

            if response.status_code == 200:
                html_content = response.text
                print(f"✅ Success! Retrieved {len(html_content)} characters of HTML")

                # Show preview
                lines = html_content.split("\n")
                preview_lines = lines[:5] if len(lines) > 5 else lines
                print("\n📝 HTML preview:")
                for line in preview_lines:
                    print(f"   {line}")
                if len(lines) > 5:
                    print("   ...")

                # Save to file
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "content.html"
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(html_content)
                print(f"💾 Saved to: {output_file}")

            else:
                print(f"❌ Request failed with status {response.status_code}")

        except Exception as e:
            print(f"💥 Error: {e}")


async def demo_error_handling():
    """Demonstrate error handling for invalid URLs."""
    print("\n❌ Demo: Error Handling")
    print("-" * 50)

    invalid_urls = [
        "invalid-url",
        "localhost:8080",
        "https://this-domain-does-not-exist.invalid",
        "https://httpbin.org/status/404",
    ]

    async with httpx.AsyncClient() as client:
        for url in invalid_urls:
            print(f"\n🌐 Testing invalid URL: {url}")

            try:
                response = await client.get(
                    f"{BASE_URL}/{url}",
                    headers={"Accept": "text/plain"},
                    timeout=10,
                )

                if response.status_code == 200:
                    print("   ✅ Unexpected success (this shouldn't happen)")
                else:
                    print(f"   ❌ Failed with status {response.status_code} (expected)")
                    try:
                        error_data = response.json()
                        detail = error_data.get("detail", {})
                        error_type = detail.get("error_type", "unknown")
                        error_msg = detail.get("error", "No error message")
                        print(f"   📋 Error type: {error_type}")
                        print(f"   📋 Error message: {error_msg}")
                    except Exception:
                        print(f"   📋 Raw error: {response.text}")

            except Exception as e:
                print(f"   💥 Exception: {e}")


async def main():
    """Main entry point for basic usage examples."""
    print("🎬 REST API Downloader - Basic Usage Examples")
    print("=" * 60)

    # Check server health first
    if not await check_server_health():
        print("\n❌ Cannot proceed with examples - server is not available")
        print("\n🔧 To start the server:")
        print("   docker build -t downloader .")
        print("   docker run -p 8000:80 downloader")
        return

    # Run all demos
    try:
        await demo_text_format()
        await demo_markdown_format()
        await demo_json_format()
        await demo_html_format()
        await demo_pdf_format()
        await demo_error_handling()

        # Summary
        print("\n" + "=" * 60)
        print("🎯 Basic Usage Examples Completed!")
        print("=" * 60)
        print(f"📁 Output files saved to: {OUTPUT_DIR}/")
        print("\n💡 Key takeaways:")
        print("   • Use Accept headers to control response format")
        print("   • Check response headers for metadata")
        print("   • Handle errors gracefully with proper status codes")
        print("   • PDF generation may have delays under load")
        print("\n📚 Next steps:")
        print("   • Try concurrent_pdf_requests.py for load testing")
        print("   • Explore content_formats.py for format comparison")
        print("   • Check error_handling.py for robust error patterns")

    except KeyboardInterrupt:
        print("\n⏹️  Examples interrupted by user")
    except Exception as e:
        print(f"\n💥 Examples failed with error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

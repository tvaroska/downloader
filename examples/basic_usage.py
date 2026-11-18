#!/usr/bin/env python3
"""Basic usage example demonstrating single-URL format negotiation."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx

BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("basic_outputs")
TIMEOUT = 30

TEST_URLS = {
    "simple": "https://example.com",
    "news": "https://news.ycombinator.com",
    "documentation": "https://docs.python.org/3/",
    "json_api": "https://httpbin.org/json",
}


async def check_server_health() -> bool:
    """Check if the server is running and return health state."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)
            if response.status_code == 200:
                health_data = response.json()
                print(f"Server healthy: version={health_data.get('version', 'unknown')}")
                return True
            print(f"Server returned status {response.status_code}")
            return False
    except (httpx.HTTPError, OSError) as exc:
        print(f"Cannot connect to server at {BASE_URL}: {exc}")
        return False


async def demo_text_format() -> None:
    """Demonstrate plain text content extraction."""
    url = TEST_URLS["news"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/plain"},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "article_text.txt"
                output_file.write_text(response.text, encoding="utf-8")
                print(f"Text extracted ({len(response.text)} chars) -> {output_file}")
            else:
                print(f"Text request failed: {response.status_code}")
        except (httpx.HTTPError, OSError) as exc:
            print(f"Text request error: {exc}")


async def demo_markdown_format() -> None:
    """Demonstrate markdown content conversion."""
    url = TEST_URLS["documentation"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/markdown"},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "content.md"
                output_file.write_text(response.text, encoding="utf-8")
                print(f"Markdown generated ({len(response.text)} chars) -> {output_file}")
            else:
                print(f"Markdown request failed: {response.status_code}")
        except (httpx.HTTPError, OSError) as exc:
            print(f"Markdown request error: {exc}")


async def demo_json_format() -> None:
    """Demonstrate JSON response with base64 content and metadata."""
    url = TEST_URLS["json_api"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                json_data = response.json()
                decoded = (
                    base64.b64decode(json_data["content"]).decode("utf-8", errors="ignore")
                    if "content" in json_data
                    else ""
                )
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "response.json"
                output_file.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
                print(f"JSON saved ({len(decoded)} decoded chars) -> {output_file}")
            else:
                print(f"JSON request failed: {response.status_code}")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            print(f"JSON request error: {exc}")


async def demo_pdf_format() -> None:
    """Demonstrate PDF generation."""
    url = TEST_URLS["simple"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "application/pdf"},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "generated.pdf"
                output_file.write_bytes(response.content)
                print(f"PDF generated ({len(response.content):,} bytes) -> {output_file}")
            else:
                print(f"PDF request returned status {response.status_code}")
        except (httpx.HTTPError, OSError) as exc:
            print(f"PDF request error: {exc}")


async def demo_html_format() -> None:
    """Demonstrate HTML content retrieval."""
    url = TEST_URLS["simple"]
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(
                f"{BASE_URL}/{url}",
                headers={"Accept": "text/html"},
                timeout=TIMEOUT,
            )
            if response.status_code == 200:
                OUTPUT_DIR.mkdir(exist_ok=True)
                output_file = OUTPUT_DIR / "content.html"
                output_file.write_text(response.text, encoding="utf-8")
                print(f"HTML retrieved ({len(response.text)} chars) -> {output_file}")
            else:
                print(f"HTML request failed: {response.status_code}")
        except (httpx.HTTPError, OSError) as exc:
            print(f"HTML request error: {exc}")


async def demo_error_handling() -> None:
    """Demonstrate error handling for invalid or restricted URLs."""
    invalid_urls = [
        "invalid-url",
        "localhost:8080",
        "https://this-domain-does-not-exist.invalid",
        "https://httpbin.org/status/404",
    ]
    async with httpx.AsyncClient() as client:
        for url in invalid_urls:
            try:
                response = await client.get(
                    f"{BASE_URL}/{url}",
                    headers={"Accept": "text/plain"},
                    timeout=10,
                )
                print(f"Tested {url} -> HTTP {response.status_code}")
            except (httpx.HTTPError, OSError) as exc:
                print(f"Tested {url} -> {exc}")


async def main() -> None:
    """Run all format demonstrations sequentially."""
    if not await check_server_health():
        return
    await demo_text_format()
    await demo_markdown_format()
    await demo_json_format()
    await demo_html_format()
    await demo_pdf_format()
    await demo_error_handling()
    print(f"Completed basic usage examples (outputs in {OUTPUT_DIR}/).")


if __name__ == "__main__":
    asyncio.run(main())

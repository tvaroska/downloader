#!/usr/bin/env python3
"""Content format comparison example requesting one URL across all output formats."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("format_comparison")
TIMEOUT = 45
TEST_URL = "https://docs.python.org/3/tutorial/introduction.html"

CONTENT_FORMATS: dict[str, dict[str, str | None]] = {
    "text": {"accept_header": "text/plain", "file_extension": "txt"},
    "markdown": {"accept_header": "text/markdown", "file_extension": "md"},
    "html": {"accept_header": "text/html", "file_extension": "html"},
    "json": {"accept_header": "application/json", "file_extension": "json"},
    "pdf": {"accept_header": "application/pdf", "file_extension": "pdf"},
    "default": {"accept_header": None, "file_extension": "txt"},
}


class FormatResult:
    """Outcome record for a single format request."""

    def __init__(self, format_name: str, accept_header: str | None) -> None:
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
        return {
            "format_name": self.format_name,
            "accept_header": self.accept_header,
            "success": self.success,
            "status_code": self.status_code,
            "duration": round(self.duration, 2),
            "content_size": self.content_size,
            "response_content_type": self.response_content_type,
            "error": self.error,
            "file_path": self.file_path,
        }


def _persist_format_payload(
    format_name: str, ext: str, response: httpx.Response
) -> tuple[int, str]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    target = OUTPUT_DIR / f"{format_name}.{ext}"
    if format_name == "json":
        json_data = response.json()
        target.write_text(json.dumps(json_data, indent=2), encoding="utf-8")
        if "content" in json_data:
            decoded = base64.b64decode(json_data["content"]).decode("utf-8", errors="ignore")
            (OUTPUT_DIR / f"{format_name}_decoded.txt").write_text(decoded, encoding="utf-8")
        return len(response.content), str(target)
    if format_name == "pdf":
        target.write_bytes(response.content)
        return len(response.content), str(target)
    target.write_text(response.text, encoding="utf-8")
    return len(response.text), str(target)


async def request_format(
    client: httpx.AsyncClient,
    format_name: str,
    format_config: dict[str, str | None],
) -> FormatResult:
    accept = format_config.get("accept_header")
    ext = format_config.get("file_extension") or "txt"
    result = FormatResult(format_name, accept)
    headers = {"Accept": accept} if accept else {}
    start_time = time.time()

    try:
        response = await client.get(f"{BASE_URL}/{TEST_URL}", headers=headers, timeout=TIMEOUT)
        result.duration = time.time() - start_time
        result.status_code = response.status_code
        result.response_content_type = response.headers.get("content-type", "")
        if response.status_code == 200:
            result.success = True
            result.content_size, result.file_path = _persist_format_payload(
                format_name, ext, response
            )
        else:
            result.error = f"HTTP {response.status_code}: {response.text[:100]}"
    except TimeoutError:
        result.error = f"Request timeout after {TIMEOUT}s"
    except (httpx.HTTPError, OSError, ValueError) as exc:
        result.error = str(exc)

    return result


async def compare_all_formats() -> dict[str, FormatResult]:
    results: dict[str, FormatResult] = {}
    async with httpx.AsyncClient() as client:
        for format_name, format_config in CONTENT_FORMATS.items():
            results[format_name] = await request_format(client, format_name, format_config)
            await asyncio.sleep(0.5)
    return results


def analyze_format_comparison(results: dict[str, FormatResult]) -> dict[str, Any]:
    successful = {k: v for k, v in results.items() if v.success}
    failed = {k: v for k, v in results.items() if not v.success}
    analysis: dict[str, Any] = {
        "total_formats": len(results),
        "successful_formats": len(successful),
        "failed_formats": len(failed),
        "success_rate": (len(successful) / len(results) * 100) if results else 0.0,
    }
    if successful:
        sizes = {k: v.content_size for k, v in successful.items()}
        durations = {k: v.duration for k, v in successful.items()}
        analysis["size_details"] = sizes
        analysis["duration_details"] = durations
    if failed:
        analysis["failures"] = {k: v.error for k, v in failed.items()}
    return analysis


async def main() -> None:
    try:
        async with httpx.AsyncClient() as client:
            health = await client.get(f"{BASE_URL}/health", timeout=5)
            if health.status_code != 200:
                print(f"Server health check failed: {health.status_code}")
                return
    except (httpx.HTTPError, OSError) as exc:
        print(f"Cannot connect to server at {BASE_URL}: {exc}")
        return

    results = await compare_all_formats()
    analysis = analyze_format_comparison(results)
    OUTPUT_DIR.mkdir(exist_ok=True)
    summary_file = OUTPUT_DIR / "format_comparison_summary.json"
    summary_file.write_text(
        json.dumps(
            {
                "test_url": TEST_URL,
                "analysis": analysis,
                "individual_results": {k: v.to_dict() for k, v in results.items()},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Format comparison summary saved to: {summary_file}")


if __name__ == "__main__":
    asyncio.run(main())

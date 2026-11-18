#!/usr/bin/env python3
"""Concurrent PDF generation example measuring latency and throughput."""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "http://localhost:8000"
OUTPUT_DIR = Path("pdf_outputs")
CONCURRENT_REQUESTS = 10
REQUEST_TIMEOUT = 60
SAVE_PDFS = True

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

    def __init__(self, url: str, start_time: float) -> None:
        self.url = url
        self.start_time = start_time
        self.end_time: float = 0.0
        self.duration: float = 0.0
        self.success: bool = False
        self.status_code: int = 0
        self.error: str = ""
        self.pdf_size: int = 0
        self.file_path: str = ""

    def finish(
        self,
        *,
        success: bool,
        status_code: int = 0,
        error: str = "",
        pdf_size: int = 0,
        file_path: str = "",
    ) -> None:
        self.end_time = time.time()
        self.duration = self.end_time - self.start_time
        self.success = success
        self.status_code = status_code
        self.error = error
        self.pdf_size = pdf_size
        self.file_path = file_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "success": self.success,
            "duration": round(self.duration, 2),
            "status_code": self.status_code,
            "error": self.error,
            "pdf_size": self.pdf_size,
            "file_path": self.file_path,
            "timestamp": datetime.fromtimestamp(self.start_time).isoformat(),
        }


async def check_server_health() -> bool:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{BASE_URL}/health", timeout=5)
            return response.status_code == 200
    except (httpx.HTTPError, OSError):
        return False


async def generate_pdf_request(
    client: httpx.AsyncClient,
    url: str,
    request_id: int,
) -> PDFRequestResult:
    result = PDFRequestResult(url, time.time())
    try:
        response = await client.get(
            f"{BASE_URL}/{url}",
            headers={"Accept": "application/pdf"},
            timeout=REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            pdf_content = response.content
            saved_path = ""
            if SAVE_PDFS:
                OUTPUT_DIR.mkdir(exist_ok=True)
                slug = url.removeprefix("https://").replace("/", "_")
                target = OUTPUT_DIR / f"request_{request_id:02d}_{slug}.pdf"
                target.write_bytes(pdf_content)
                saved_path = str(target)
            result.finish(
                success=True,
                status_code=200,
                pdf_size=len(pdf_content),
                file_path=saved_path,
            )
            return result

        try:
            error_msg = (
                response.json().get("detail", {}).get("error", f"HTTP {response.status_code}")
            )
        except (ValueError, AttributeError):
            error_msg = f"HTTP {response.status_code}: {response.text[:100]}"
        result.finish(success=False, status_code=response.status_code, error=str(error_msg))
    except TimeoutError:
        result.finish(success=False, error="Request timeout")
    except (httpx.HTTPError, OSError) as exc:
        result.finish(success=False, error=str(exc))
    return result


async def run_concurrent_pdf_test() -> list[PDFRequestResult]:
    limits = httpx.Limits(max_keepalive_connections=20, max_connections=100)
    timeout = httpx.Timeout(REQUEST_TIMEOUT)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        tasks = [
            generate_pdf_request(client, TEST_URLS[i % len(TEST_URLS)], i + 1)
            for i in range(CONCURRENT_REQUESTS)
        ]
        return list(await asyncio.gather(*tasks))


def analyze_results(results: list[PDFRequestResult]) -> dict[str, Any]:
    successful = [r for r in results if r.success]
    failed = [r for r in results if not r.success]
    analysis: dict[str, Any] = {
        "total_requests": len(results),
        "successful_requests": len(successful),
        "failed_requests": len(failed),
        "success_rate": (len(successful) / len(results) * 100) if results else 0.0,
    }
    if successful:
        durations = [r.duration for r in successful]
        sizes = [r.pdf_size for r in successful]
        analysis["timing_stats"] = {
            "min_duration": min(durations),
            "max_duration": max(durations),
            "avg_duration": statistics.mean(durations),
            "median_duration": statistics.median(durations),
            "total_duration": sum(durations),
        }
        analysis["size_stats"] = {
            "min_pdf_size": min(sizes),
            "max_pdf_size": max(sizes),
            "avg_pdf_size": statistics.mean(sizes),
            "total_pdf_size": sum(sizes),
        }
    return analysis


async def main() -> None:
    if not await check_server_health():
        print(f"Server at {BASE_URL} is not reachable.")
        return

    results = await run_concurrent_pdf_test()
    analysis = analyze_results(results)
    OUTPUT_DIR.mkdir(exist_ok=True)
    results_file = (
        OUTPUT_DIR / f"concurrent_pdf_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    results_file.write_text(
        json.dumps(
            {
                "analysis": analysis,
                "individual_results": [r.to_dict() for r in results],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"Completed {analysis['total_requests']} concurrent PDF requests "
        f"({analysis['success_rate']:.1f}% ok) -> {results_file}"
    )


if __name__ == "__main__":
    asyncio.run(main())

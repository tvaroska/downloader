"""Unit tests for R30: browser semaphore capacity matches browser pool size."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from downloader.config import PDFConfig, Settings
from downloader.errors import classify_domain_error
from downloader.ports import BrowserCapacityError, LocalBrowserRenderer


@pytest.mark.unit
def test_default_pdf_concurrency_matches_pool_size() -> None:
    """R30: Default PDFConfig concurrency tracks pool_size instead of 2x CPU cores."""
    default_cfg = PDFConfig(_env_file=None)
    assert default_cfg.concurrency == default_cfg.pool_size == 3

    sized_cfg = PDFConfig(_env_file=None, pool_size=5)
    assert sized_cfg.concurrency == 5

    explicit_cfg = PDFConfig(_env_file=None, pool_size=3, concurrency=7)
    assert explicit_cfg.concurrency == 7


@pytest.mark.unit
async def test_fourth_concurrent_render_gets_503_not_500() -> None:
    """R30: With default pool_size=3, a 4th concurrent render fails fast with 503 BrowserCapacityError."""
    cfg = Settings(_env_file=None)
    assert cfg.pdf.pool_size == 3
    assert cfg.pdf.concurrency == 3

    pdf_semaphore = asyncio.Semaphore(cfg.pdf.concurrency)
    renderer = LocalBrowserRenderer(pdf_semaphore=pdf_semaphore)

    three_entered = asyncio.Event()
    release_renders = asyncio.Event()
    in_flight = 0

    async def _slow_pdf(*_args: object, **_kwargs: object) -> bytes:
        nonlocal in_flight
        in_flight += 1
        if in_flight == 3:
            three_entered.set()
        await release_renders.wait()
        return b"%PDF-1.7 ok"

    with patch(
        "downloader.pdf_generator.generate_pdf_from_url", new=AsyncMock(side_effect=_slow_pdf)
    ):
        tasks = [
            asyncio.create_task(renderer.render_pdf(f"https://example.com/{i}")) for i in range(3)
        ]
        await three_entered.wait()

        with pytest.raises(BrowserCapacityError) as exc_info:
            await renderer.render_pdf("https://example.com/4")

        classified = classify_domain_error(exc_info.value)
        assert classified.status_code == 503
        assert classified.error_type == "service_unavailable"

        release_renders.set()
        results = await asyncio.gather(*tasks)
        assert all(r == b"%PDF-1.7 ok" for r in results)

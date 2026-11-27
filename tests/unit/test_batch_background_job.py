"""Unit tests for background batch job orchestration and summaries."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from downloader.http_client import HTTPClientError
from downloader.job_manager import JobStatus
from downloader.models.responses import BatchRequest, BatchURLRequest
from downloader.services.batch_processor import (
    process_background_batch_job,
)


class TestProcessBackgroundBatchJob:
    """Unit tests for process_background_batch_job function."""

    @pytest.fixture
    def mock_http_client(self):
        """Create a mock HTTP client."""
        mock_client = AsyncMock()
        mock_client.download.return_value = (
            b"<html><body><p>Hello World</p></body></html>",
            {
                "url": "https://example.com",
                "content_type": "text/html",
                "size": 45,
            },
        )
        return mock_client

    @pytest.fixture
    def mock_job_manager(self):
        """Create a mock job manager."""
        mock_manager = AsyncMock()
        mock_manager.update_job_status = AsyncMock()
        return mock_manager

    @pytest.fixture
    def batch_semaphore(self):
        """Create a batch semaphore."""
        return asyncio.Semaphore(10)

    @pytest.fixture
    def pdf_semaphore(self):
        """Create a PDF semaphore."""
        return asyncio.Semaphore(1)

    @pytest.mark.asyncio
    async def test_all_urls_succeed(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test batch job with all URLs succeeding."""
        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
                BatchURLRequest(url="https://example3.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-1",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert len(results) == 3
        assert all(r["success"] for r in results)
        assert summary["total_requests"] == 3
        assert summary["successful_requests"] == 3
        assert summary["failed_requests"] == 0
        assert summary["success_rate"] == 100.0
        assert summary["total_duration"] > 0

        assert mock_job_manager.update_job_status.call_count == 3
        final_kwargs = mock_job_manager.update_job_status.call_args.kwargs
        assert final_kwargs["progress"] == 100
        assert final_kwargs["processed_urls"] == 3
        assert final_kwargs["successful_urls"] == 3
        assert final_kwargs["failed_urls"] == 0

    @pytest.mark.asyncio
    async def test_all_urls_fail(self, mock_job_manager, batch_semaphore, pdf_semaphore):
        """Test batch job with all URLs failing."""
        mock_http_client = AsyncMock()
        mock_http_client.download.side_effect = HTTPClientError("Not Found", status_code=404)

        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://fail1.com"),
                BatchURLRequest(url="https://fail2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-2",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert len(results) == 2
        assert all(not r["success"] for r in results)
        assert summary["total_requests"] == 2
        assert summary["successful_requests"] == 0
        assert summary["failed_requests"] == 2
        assert summary["success_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_partial_failure(self, mock_job_manager, batch_semaphore, pdf_semaphore):
        """Test batch job with mix of success and failure."""

        async def mock_download(url, priority=None, **_kwargs):
            if "fail" in url:
                raise HTTPClientError("Not Found", status_code=404)
            return (
                b"<html>Content</html>",
                {"url": url, "content_type": "text/html", "size": 20},
            )

        mock_http_client = AsyncMock()
        mock_http_client.download = mock_download

        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://success1.com"),
                BatchURLRequest(url="https://fail.com"),
                BatchURLRequest(url="https://success2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        with patch(
            "downloader.services.batch_processor.validate_url_async",
            new_callable=AsyncMock,
            side_effect=lambda url, *_args: url,
        ):
            results, summary = await process_background_batch_job(
                job_id="test-job-3",
                batch_request=batch_request,
                job_manager=mock_job_manager,
                batch_semaphore=batch_semaphore,
                http_client=mock_http_client,
                pdf_semaphore=pdf_semaphore,
            )

        assert len(results) == 3
        assert summary["total_requests"] == 3
        assert summary["successful_requests"] == 2
        assert summary["failed_requests"] == 1
        assert abs(summary["success_rate"] - 66.67) < 1

    @pytest.mark.asyncio
    async def test_job_status_updated(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test that job status is updated correctly."""
        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example.com"),
                BatchURLRequest(url="https://test.com"),
                BatchURLRequest(url="https://success.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        await process_background_batch_job(
            job_id="test-job-4",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert mock_job_manager.update_job_status.call_count == 3
        final_kwargs = mock_job_manager.update_job_status.call_args.kwargs
        assert final_kwargs["progress"] == 100
        assert final_kwargs["processed_urls"] == 3
        assert final_kwargs["successful_urls"] == 3
        assert final_kwargs["failed_urls"] == 0
        call_args = mock_job_manager.update_job_status.call_args
        assert call_args[0][0] == "test-job-4"
        assert call_args[0][1] == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_results_aggregation(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test that results are properly aggregated."""
        batch_request = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
            ],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-5",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert isinstance(results, list)
        assert all(isinstance(r, dict) for r in results)
        assert all("url" in r for r in results)
        assert all("success" in r for r in results)

        assert "total_requests" in summary
        assert "successful_requests" in summary
        assert "failed_requests" in summary
        assert "success_rate" in summary
        assert "total_duration" in summary

    @pytest.mark.asyncio
    async def test_single_url_batch(
        self, mock_http_client, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """Test batch job with single URL."""
        batch_request = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=30,
        )

        results, summary = await process_background_batch_job(
            job_id="test-job-6",
            batch_request=batch_request,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=mock_http_client,
            pdf_semaphore=pdf_semaphore,
        )

        assert len(results) == 1
        assert summary["total_requests"] == 1

    @pytest.mark.asyncio
    async def test_concurrency_limit_bounds_in_flight(self, mock_job_manager, pdf_semaphore):
        """Both per-job `concurrency_limit` and global `batch_semaphore` bound concurrent fetches (`R92`)."""
        active = 0
        peak = 0

        async def _counting_download(url: str, **_kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return (
                b"<html><body>ok</body></html>",
                {"url": url, "content_type": "text/html", "size": 28},
            )

        counting_http = AsyncMock()
        counting_http.download.side_effect = _counting_download

        req = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(6)],
            default_format="html",
            concurrency_limit=2,
            timeout_per_url=10,
        )
        results, summary = await process_background_batch_job(
            job_id="job-conc-1",
            batch_request=req,
            job_manager=mock_job_manager,
            batch_semaphore=asyncio.Semaphore(10),
            http_client=counting_http,
            pdf_semaphore=pdf_semaphore,
        )
        assert summary["successful_requests"] == 6
        assert len(results) == 6
        assert peak == 2

        # Global batch_semaphore=1 must further cap concurrency even when concurrency_limit=5.
        peak = 0
        req_wide = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(4)],
            default_format="html",
            concurrency_limit=5,
            timeout_per_url=10,
        )
        await process_background_batch_job(
            job_id="job-conc-2",
            batch_request=req_wide,
            job_manager=mock_job_manager,
            batch_semaphore=asyncio.Semaphore(1),
            http_client=counting_http,
            pdf_semaphore=pdf_semaphore,
        )
        assert peak == 1

    @pytest.mark.asyncio
    async def test_timeout_per_url_is_enforced(
        self, mock_job_manager, batch_semaphore, pdf_semaphore
    ):
        """A URL exceeding `timeout_per_url` fails with `408 timeout_error` while peers succeed (`R92`)."""

        async def _slow_or_fast(url: str, **_kwargs):
            if "example1" in url:
                await asyncio.sleep(5.0)
            return (
                b"<html><body>fast</body></html>",
                {"url": url, "content_type": "text/html", "size": 30},
            )

        http_client = AsyncMock()
        http_client.download.side_effect = _slow_or_fast

        req = BatchRequest(
            urls=[
                BatchURLRequest(url="https://example1.com"),
                BatchURLRequest(url="https://example2.com"),
            ],
            default_format="html",
            concurrency_limit=2,
            timeout_per_url=5,
        )
        # Bypass Pydantic `ge=5` floor in-test so `asyncio.wait_for` triggers quickly.
        object.__setattr__(req, "timeout_per_url", 0.05)
        results, summary = await process_background_batch_job(
            job_id="job-timeout",
            batch_request=req,
            job_manager=mock_job_manager,
            batch_semaphore=batch_semaphore,
            http_client=http_client,
            pdf_semaphore=pdf_semaphore,
        )
        assert summary["successful_requests"] == 1
        assert summary["failed_requests"] == 1
        by_url = {r["url"]: r for r in results}
        assert by_url["https://example1.com"]["success"] is False
        assert by_url["https://example1.com"]["status_code"] == 408
        assert by_url["https://example1.com"]["error_type"] == "timeout_error"
        assert by_url["https://example2.com"]["success"] is True

    @pytest.mark.asyncio
    async def test_cancelled_job_stops_before_next_url(self, batch_semaphore, pdf_semaphore):
        """A job marked CANCELLED in storage halts before starting remaining URLs (`R92`)."""
        from tests.fixtures.in_memory_ports import InMemoryJobStore

        store = InMemoryJobStore()
        job_id = await store.create_job(
            {"urls": [{"url": f"https://example{i}.com"} for i in range(4)]}
        )
        await store.update_job_status(job_id, JobStatus.RUNNING)

        fetched: list[str] = []

        async def _cancel_after_first(url: str, **_kwargs):
            fetched.append(url)
            if len(fetched) == 1:
                await store.update_job_status(job_id, JobStatus.CANCELLED)
            return (
                b"<html><body>ok</body></html>",
                {"url": url, "content_type": "text/html", "size": 28},
            )

        http_client = AsyncMock()
        http_client.download.side_effect = _cancel_after_first

        req = BatchRequest(
            urls=[BatchURLRequest(url=f"https://example{i}.com") for i in range(4)],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=10,
        )
        with pytest.raises(asyncio.CancelledError):
            await process_background_batch_job(
                job_id=job_id,
                batch_request=req,
                job_manager=store,
                batch_semaphore=batch_semaphore,
                http_client=http_client,
                pdf_semaphore=pdf_semaphore,
            )
        assert fetched == ["https://example0.com"]

    @pytest.mark.asyncio
    async def test_crawl_delay_outside_semaphore_and_progress_redis_error_tolerated(
        self, pdf_semaphore
    ):
        """Crawl-delay sleep runs outside `batch_semaphore` (`R114`) and Redis progress errors don't abort (`R85`)."""
        import redis.exceptions as redis_exc

        batch_sem = asyncio.Semaphore(1)
        sem_value_during_throttle: list[int] = []

        class _SpyThrottle:
            async def wait_if_needed(self, _domain: str, _delay: float | None) -> float:
                sem_value_during_throttle.append(batch_sem._value)
                return 0.0

        class _SpyRobots:
            async def check_allowed(self, _url: str, _ua: str | None = None) -> float | None:
                return 0.25

        http_client = AsyncMock()
        http_client.download.return_value = (
            b"<html><body>ok</body></html>",
            {"url": "https://example.com", "content_type": "text/html", "size": 28},
        )
        flaky_jm = AsyncMock()
        flaky_jm.get_job_info = AsyncMock(return_value=None)
        flaky_jm.update_job_status = AsyncMock(
            side_effect=redis_exc.ConnectionError("transient progress write error")
        )

        req = BatchRequest(
            urls=[BatchURLRequest(url="https://example.com")],
            default_format="html",
            concurrency_limit=1,
            timeout_per_url=10,
        )
        from typing import Any, cast

        results, summary = await process_background_batch_job(
            job_id="job-resilient",
            batch_request=req,
            job_manager=flaky_jm,
            batch_semaphore=batch_sem,
            http_client=http_client,
            pdf_semaphore=pdf_semaphore,
            robots_checker=cast(Any, _SpyRobots()),
            domain_throttle=cast(Any, _SpyThrottle()),
        )
        assert sem_value_during_throttle == [1]
        assert summary["successful_requests"] == 1
        assert len(results) == 1

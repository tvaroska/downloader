"""Job executor for scheduled download jobs.

This module provides the execution logic for scheduled downloads, including
retry handling, content processing, and result storage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..config import get_settings
from ..content_converter import convert_content_to_markdown, convert_content_to_text
from ..crawling import DomainThrottle, RobotsChecker
from ..http_client import RequestPriority
from ..models.schedule import ExecutionStatus, ScheduleExecution
from ..pdf_generator import generate_pdf_from_url, generate_screenshot_from_url
from ..services.webhooks import deliver_webhook
from ..transformers.ocr import (
    extract_text_from_image,
    format_ocr_markdown,
    is_ocr_image_content_type,
)
from ..validation import sanitize_custom_headers, validate_url_async

if TYPE_CHECKING:
    from ..http_client import HTTPClient
    from ..models.responses import ResponseMetadata
    from ..pdf_generator import PlaywrightPDFGenerator
    from .storage import ExecutionStorage

logger = logging.getLogger(__name__)

# Retry configuration
MAX_ATTEMPTS = 3
RETRY_DELAYS = [5, 15, 30]  # seconds between retries
_NON_RETRYABLE_PREFIXES = (
    "URLValidationError:",
    "SSRFProtectionError:",
    "RobotsDisallowedError:",
)


class ScheduledJobExecutor:
    """Executes scheduled download jobs with retry and result storage."""

    def __init__(
        self,
        http_client: HTTPClient,
        storage: ExecutionStorage,
        pdf_generator: PlaywrightPDFGenerator | None = None,
        pdf_semaphore: asyncio.Semaphore | None = None,
        robots_checker: RobotsChecker | None = None,
        domain_throttle: DomainThrottle | None = None,
        max_workers: int = 10,
    ) -> None:
        self.http_client = http_client
        self.storage = storage
        self.pdf_generator = pdf_generator
        self.pdf_semaphore = pdf_semaphore
        self.robots_checker = robots_checker
        self.domain_throttle = domain_throttle
        self.max_workers = max(1, max_workers)
        self._worker_semaphore = asyncio.Semaphore(self.max_workers)

    async def _emit_schedule_webhook(
        self,
        execution: ScheduleExecution,
        url: str,
        format: str,
        webhook_url: str | None,
        webhook_secret: str | None,
    ) -> None:
        if not webhook_url:
            return
        event = "schedule.completed" if execution.success else "schedule.failed"
        await deliver_webhook(
            webhook_url,
            event,
            {
                "event": event,
                "schedule_id": execution.schedule_id,
                "execution_id": execution.execution_id,
                "url": url,
                "format": format,
                "status": execution.status.value,
                "success": execution.success,
                "content_size": execution.content_size,
                "duration": execution.duration,
                "attempt": execution.attempt,
                "error_message": execution.error_message,
            },
            secret=webhook_secret,
            settings=get_settings(),
        )

    async def execute(
        self,
        schedule_id: str,
        url: str,
        format: str,
        headers: dict[str, str] | None,
        *,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> ScheduleExecution:
        """Execute a scheduled download with retry logic and optional webhook notification."""
        execution_id = str(uuid.uuid4())
        attempt = 0
        execution: ScheduleExecution | None = None

        while attempt < MAX_ATTEMPTS:
            attempt += 1

            execution = await self._execute_single_attempt(
                execution_id=execution_id,
                schedule_id=schedule_id,
                url=url,
                format=format,
                headers=headers,
                attempt=attempt,
            )

            await self.storage.store_execution(execution)

            if execution.success:
                await self._emit_schedule_webhook(
                    execution, url, format, webhook_url, webhook_secret
                )
                return execution

            if execution.error_message and execution.error_message.startswith(
                _NON_RETRYABLE_PREFIXES
            ):
                logger.info(
                    f"[SCHEDULE-{schedule_id}] Non-retryable policy error on attempt {attempt}: "
                    f"{execution.error_message}"
                )
                await self._emit_schedule_webhook(
                    execution, url, format, webhook_url, webhook_secret
                )
                return execution

            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[attempt - 1]
                logger.info(
                    f"[SCHEDULE-{schedule_id}] Attempt {attempt}/{MAX_ATTEMPTS} failed, "
                    f"retrying in {delay}s: {execution.error_message}"
                )
                await asyncio.sleep(delay)

        logger.error(
            f"[SCHEDULE-{schedule_id}] All {MAX_ATTEMPTS} attempts failed: "
            f"{execution.error_message if execution else 'Unknown error'}"
        )

        assert execution is not None
        await self._emit_schedule_webhook(execution, url, format, webhook_url, webhook_secret)
        return execution

    async def _execute_single_attempt(
        self,
        execution_id: str,
        schedule_id: str,
        url: str,
        format: str,
        headers: dict[str, str] | None,
        attempt: int,
    ) -> ScheduleExecution:
        """Execute a single download attempt bounded by the worker semaphore."""
        async with self._worker_semaphore:
            started_at = datetime.now(UTC)

            try:
                validated_url = await validate_url_async(url)
                sanitized_headers = sanitize_custom_headers(headers)

                cfg = get_settings()
                if self.robots_checker is not None and cfg.crawling.respect_robots_txt:
                    effective_ua = next(
                        (
                            v
                            for k, v in (sanitized_headers or {}).items()
                            if k.lower() == "user-agent"
                        ),
                        cfg.crawling.user_agent,
                    )
                    crawl_delay = await self.robots_checker.check_allowed(
                        validated_url, effective_ua
                    )
                    if self.domain_throttle is not None:
                        await self.domain_throttle.wait_if_needed(
                            urlparse(validated_url).netloc, crawl_delay
                        )

                if format in {"pdf", "screenshot"}:
                    processed = await self._process_content(
                        url=validated_url,
                        content=b"",
                        metadata=None,
                        format=format,
                    )
                else:
                    if sanitized_headers:
                        content, metadata = await self.http_client.download(
                            validated_url, RequestPriority.LOW, headers=sanitized_headers
                        )
                    else:
                        content, metadata = await self.http_client.download(
                            validated_url, RequestPriority.LOW
                        )

                    processed = await self._process_content(
                        url=validated_url,
                        content=content,
                        metadata=metadata,
                        format=format,
                    )

                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()

                if isinstance(processed, str):
                    content_size: int | None = len(processed.encode("utf-8"))
                elif isinstance(processed, bytes):
                    content_size = len(processed)
                else:
                    content_size = None

                return ScheduleExecution(
                    execution_id=execution_id,
                    schedule_id=schedule_id,
                    status=ExecutionStatus.COMPLETED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration=duration,
                    success=True,
                    content_size=content_size,
                    error_message=None,
                    attempt=attempt,
                )

            except Exception as e:
                completed_at = datetime.now(UTC)
                duration = (completed_at - started_at).total_seconds()

                logger.warning(
                    f"[SCHEDULE-{schedule_id}] Attempt {attempt} failed: {type(e).__name__}: {e}"
                )

                return ScheduleExecution(
                    execution_id=execution_id,
                    schedule_id=schedule_id,
                    status=ExecutionStatus.FAILED,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration=duration,
                    success=False,
                    content_size=None,
                    error_message=f"{type(e).__name__}: {e}",
                    attempt=attempt,
                )

    async def _process_content(
        self,
        url: str,
        content: bytes,
        metadata: ResponseMetadata | Mapping[str, Any] | None,
        format: str,
    ) -> str | bytes:
        """Process downloaded content based on output format."""
        meta = metadata or {}
        content_type = meta.get("content_type", "application/octet-stream")

        if format == "text":
            if is_ocr_image_content_type(content_type):
                return await extract_text_from_image(content)
            return convert_content_to_text(content, content_type)

        elif format == "markdown":
            if is_ocr_image_content_type(content_type):
                ocr_text = await extract_text_from_image(content)
                return format_ocr_markdown(ocr_text, str(meta.get("url", url)))
            return convert_content_to_markdown(content, content_type)

        elif format == "html":
            return content.decode("utf-8", errors="ignore")

        elif format == "pdf":
            if self.pdf_semaphore is None:
                raise RuntimeError("PDF generation not available: semaphore not configured")
            async with self.pdf_semaphore:
                return await generate_pdf_from_url(url)

        elif format == "screenshot":
            if self.pdf_semaphore is None:
                raise RuntimeError("Screenshot capture not available: semaphore not configured")
            async with self.pdf_semaphore:
                return await generate_screenshot_from_url(url, {"type": "png", "full_page": False})

        elif format == "json":
            content_b64 = base64.b64encode(content).decode("utf-8")
            return json.dumps(
                {
                    "success": True,
                    "url": meta.get("url", url),
                    "size": meta.get("size", len(content)),
                    "content_type": content_type,
                    "content": content_b64,
                    "metadata": dict(meta),
                }
            )

        elif format == "raw":
            return content

        else:
            logger.warning(f"Unknown format '{format}', returning raw content")
            return content

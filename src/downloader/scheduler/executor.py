"""Job executor for scheduled download jobs.

This module provides the execution logic for scheduled downloads, including
retry handling, content processing, and result storage.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from ..content_converter import convert_content_to_markdown, convert_content_to_text
from ..http_client import RequestPriority
from ..models.schedule import ExecutionStatus, ScheduleExecution
from ..pdf_generator import generate_pdf_from_url
from ..validation import validate_url

if TYPE_CHECKING:
    from ..http_client import HTTPClient
    from ..pdf_generator import PlaywrightPDFGenerator
    from .storage import ExecutionStorage

logger = logging.getLogger(__name__)

# Retry configuration
MAX_ATTEMPTS = 3
RETRY_DELAYS = [5, 15, 30]  # seconds between retries


class ScheduledJobExecutor:
    """Executes scheduled download jobs with retry and result storage.

    This class handles the actual download execution when APScheduler triggers
    a scheduled job. It includes:
    - Download execution with timeout
    - Content format conversion (text, markdown, html, pdf, json, raw)
    - Retry logic with exponential backoff
    - Execution result storage in Redis

    Attributes:
        http_client: HTTP client for downloading content.
        storage: Redis storage for execution records.
        pdf_generator: Optional PDF generator for PDF format.
        pdf_semaphore: Optional semaphore for PDF concurrency control.
    """

    def __init__(
        self,
        http_client: HTTPClient,
        storage: ExecutionStorage,
        pdf_generator: PlaywrightPDFGenerator | None = None,
        pdf_semaphore: asyncio.Semaphore | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            http_client: HTTP client for downloading content.
            storage: Redis storage for execution records.
            pdf_generator: Optional PDF generator for PDF format.
            pdf_semaphore: Optional semaphore for PDF concurrency control.
        """
        self.http_client = http_client
        self.storage = storage
        self.pdf_generator = pdf_generator
        self.pdf_semaphore = pdf_semaphore

    async def execute(
        self,
        schedule_id: str,
        url: str,
        format: str,
        headers: dict[str, str] | None,
    ) -> ScheduleExecution:
        """Execute a scheduled download with retry logic.

        Attempts to download the URL up to MAX_ATTEMPTS times with exponential
        backoff between retries. Each attempt is stored in the execution history.

        Args:
            schedule_id: The schedule identifier.
            url: URL to download.
            format: Output format (text, markdown, html, pdf, json, raw).
            headers: Optional custom HTTP headers.

        Returns:
            The final execution result (success or final failure).
        """
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

            # Store every attempt in history
            await self.storage.store_execution(execution)

            if execution.success:
                return execution

            # Don't retry on the last attempt
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[attempt - 1]
                logger.info(
                    f"[SCHEDULE-{schedule_id}] Attempt {attempt}/{MAX_ATTEMPTS} failed, "
                    f"retrying in {delay}s: {execution.error_message}"
                )
                await asyncio.sleep(delay)

        # All attempts failed
        logger.error(
            f"[SCHEDULE-{schedule_id}] All {MAX_ATTEMPTS} attempts failed: "
            f"{execution.error_message if execution else 'Unknown error'}"
        )

        # execution is guaranteed to be set here since we enter the loop at least once
        assert execution is not None
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
        """Execute a single download attempt.

        Args:
            execution_id: Unique identifier for this execution.
            schedule_id: The schedule identifier.
            url: URL to download.
            format: Output format.
            headers: Optional custom HTTP headers.
            attempt: Current attempt number (1-based).

        Returns:
            Execution record with success or failure status.
        """
        started_at = datetime.now(timezone.utc)

        try:
            # Validate URL
            validated_url = validate_url(url)

            # Download content
            content, metadata = await self.http_client.download(validated_url, RequestPriority.LOW)

            # Process content based on format
            processed = await self._process_content(
                url=validated_url,
                content=content,
                metadata=metadata,
                format=format,
            )

            completed_at = datetime.now(timezone.utc)
            duration = (completed_at - started_at).total_seconds()

            # Calculate content size
            if isinstance(processed, str):
                content_size = len(processed.encode("utf-8"))
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
            completed_at = datetime.now(timezone.utc)
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
        metadata: dict[str, Any],
        format: str,
    ) -> str | bytes:
        """Process downloaded content based on output format.

        Mirrors the content processing logic from routes/batch.py for consistency.

        Args:
            url: The validated URL that was downloaded.
            content: Raw downloaded content.
            metadata: Download metadata including content_type.
            format: Output format.

        Returns:
            Processed content as string or bytes.

        Raises:
            RuntimeError: If PDF generation is requested but not available.
        """
        content_type = metadata.get("content_type", "application/octet-stream")

        if format == "text":
            return convert_content_to_text(content, content_type)

        elif format == "markdown":
            return convert_content_to_markdown(content, content_type)

        elif format == "html":
            return content.decode("utf-8", errors="ignore")

        elif format == "pdf":
            if self.pdf_semaphore is None:
                raise RuntimeError("PDF generation not available: semaphore not configured")
            async with self.pdf_semaphore:
                return await generate_pdf_from_url(url)

        elif format == "json":
            # Return raw content as bytes (caller can encode to base64)
            return content

        elif format == "raw":
            return content

        else:
            # Unknown format - return raw content
            logger.warning(f"Unknown format '{format}', returning raw content")
            return content

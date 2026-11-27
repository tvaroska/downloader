"""Job executor for scheduled downloads delegating to `convert()` and `classify_domain_error()`."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from ..config import Settings, get_settings
from ..crawling import DomainThrottle, RobotsChecker
from ..errors import classify_domain_error
from ..http_client import HTTPClient
from ..models.responses import ResponseMetadata
from ..models.schedule import ExecutionStatus, ScheduleExecution
from ..ports import BrowserRenderer
from ..services.content_processor import ConversionDeps, ConversionRequest, OutputFormat, convert
from ..services.webhooks import deliver_webhook, open_webhook_secret
from ..validation import (
    EgressPolicy,
    canonical_host,
    redact_url_for_log,
    sanitize_custom_headers,
    validate_url_async,
)

if TYPE_CHECKING:
    from ..pdf_generator import PlaywrightPDFGenerator
    from .storage import ExecutionStorage

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS = [5, 15, 30]


def _coerce_metadata(
    url: str,
    content: bytes,
    metadata: ResponseMetadata | Mapping[str, Any] | None,
) -> ResponseMetadata:
    raw = dict(metadata) if metadata is not None else {}
    return {
        "status_code": int(raw.get("status_code", 200)),
        "headers": dict(raw.get("headers") or {}),
        "url": str(raw.get("url", url)),
        "size": int(raw.get("size", len(content))),
        "content_type": str(raw.get("content_type", "application/octet-stream")),
        "http_version": str(raw.get("http_version", "HTTP/1.1")),
        "connection_reused": raw.get("connection_reused"),
    }


class ScheduledJobExecutor:
    """Executes scheduled download jobs with bounded concurrency, retries, and history storage."""

    def __init__(  # noqa: PLR0913
        self,
        http_client: Any,
        storage: ExecutionStorage,
        pdf_generator: PlaywrightPDFGenerator | None = None,
        pdf_semaphore: asyncio.Semaphore | None = None,
        robots_checker: RobotsChecker | None = None,
        domain_throttle: DomainThrottle | None = None,
        max_workers: int = 10,
        *,
        renderer: BrowserRenderer | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.http_client = http_client
        self.storage = storage
        self.pdf_generator = pdf_generator
        self.pdf_semaphore = pdf_semaphore
        self.robots_checker = robots_checker
        self.domain_throttle = domain_throttle
        self.max_workers = max(1, max_workers)
        self._worker_semaphore = asyncio.Semaphore(self.max_workers)
        self._renderer = renderer
        self._settings = settings

    def _resolve_renderer(self, *, require_semaphore: bool = True) -> BrowserRenderer:
        if self._renderer is not None:
            return self._renderer
        if require_semaphore and self.pdf_semaphore is None:
            raise RuntimeError("Browser rendering not available: semaphore not configured")
        from ..browser.renderer import LocalBrowserRenderer

        cfg = self._settings or get_settings()
        return LocalBrowserRenderer(
            self.pdf_semaphore,
            egress_policy=EgressPolicy(cfg),
            max_output_bytes=cfg.content.max_download_size,
        )

    async def _emit_schedule_webhook(
        self,
        execution: ScheduleExecution,
        url: str,
        format: str,
        webhook_url: str | None,
        resolved_secret: str | None,
    ) -> None:
        if not webhook_url:
            return
        cfg = self._settings or get_settings()
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
            secret=resolved_secret,
            settings=cfg,
        )

    async def execute(  # noqa: PLR0913
        self,
        schedule_id: str,
        url: str,
        format: str,
        headers: dict[str, str] | None,
        *,
        webhook_url: str | None = None,
        webhook_secret: str | None = None,
    ) -> ScheduleExecution:
        """Execute a scheduled download with retry logic driven by `classify_domain_error`."""
        execution_id = str(uuid.uuid4())
        cfg = self._settings or get_settings()
        try:
            resolved_secret = open_webhook_secret(webhook_secret, cfg)
        except ValueError as exc:
            now = datetime.now(UTC)
            failed_exec = ScheduleExecution(
                execution_id=execution_id,
                schedule_id=schedule_id,
                status=ExecutionStatus.FAILED,
                started_at=now,
                completed_at=now,
                duration=0.0,
                success=False,
                content_size=None,
                error_message=f"ValueError: {exc}",
                attempt=1,
            )
            await self.storage.store_execution(failed_exec)
            logger.error(f"[SCHEDULE-{schedule_id}] Failed decrypting stored webhook secret: {exc}")
            return failed_exec

        execution: ScheduleExecution | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            execution, retryable = await self._execute_single_attempt(
                (execution_id, schedule_id),
                (url, format, headers),
                attempt=attempt,
            )
            await self.storage.store_execution(execution)

            if execution.success or not retryable:
                if not execution.success:
                    logger.info(
                        f"[SCHEDULE-{schedule_id}] Non-retryable error on attempt {attempt}: "
                        f"{execution.error_message}"
                    )
                await self._emit_schedule_webhook(
                    execution, url, format, webhook_url, resolved_secret
                )
                return execution

            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAYS[attempt - 1]
                logger.info(
                    f"[SCHEDULE-{schedule_id}] Attempt {attempt}/{MAX_ATTEMPTS} failed, "
                    f"retrying in {delay}s: {execution.error_message}"
                )
                await asyncio.sleep(delay)

        assert execution is not None
        logger.error(
            f"[SCHEDULE-{schedule_id}] All {MAX_ATTEMPTS} attempts failed: {execution.error_message}"
        )
        await self._emit_schedule_webhook(execution, url, format, webhook_url, resolved_secret)
        return execution

    async def _enforce_crawling_policy(
        self,
        validated_url: str,
        sanitized_headers: dict[str, str] | None,
        cfg: Settings,
    ) -> None:
        if self.robots_checker is None or not cfg.crawling.respect_robots_txt:
            return
        effective_ua = next(
            (v for k, v in (sanitized_headers or {}).items() if k.lower() == "user-agent"),
            cfg.crawling.user_agent,
        )
        crawl_delay = await self.robots_checker.check_allowed(validated_url, effective_ua)
        if self.domain_throttle is not None:
            await self.domain_throttle.wait_if_needed(canonical_host(validated_url), crawl_delay)

    async def _download_for_schedule(
        self,
        validated_url: str,
        sanitized_headers: dict[str, str] | None,
        format: str,
    ) -> tuple[bytes, ResponseMetadata]:
        if isinstance(self.http_client, HTTPClient):
            return await self.http_client.download(
                validated_url,
                headers=sanitized_headers,
                require_text_convertible=format in {"text", "markdown", "html"},
                allow_ocr_images=format in {"text", "markdown", "html"},
                target_format=format,
            )
        if sanitized_headers:
            res_with_headers: tuple[bytes, ResponseMetadata] = await self.http_client.download(
                validated_url, headers=sanitized_headers
            )
            return res_with_headers
        res_plain: tuple[bytes, ResponseMetadata] = await self.http_client.download(validated_url)
        return res_plain

    async def _execute_single_attempt(
        self,
        ids: tuple[str, str],
        target: tuple[str, str, dict[str, str] | None],
        attempt: int,
    ) -> tuple[ScheduleExecution, bool]:
        execution_id, schedule_id = ids
        url, format, headers = target
        async with self._worker_semaphore:
            started_at = datetime.now(UTC)
            cfg = self._settings or get_settings()
            try:
                validated_url = await validate_url_async(url, cfg)
                sanitized_headers = sanitize_custom_headers(headers)
                await self._enforce_crawling_policy(validated_url, sanitized_headers, cfg)

                if format in {"pdf", "screenshot"}:
                    processed = await self._process_content(
                        url=validated_url,
                        content=b"",
                        metadata=None,
                        format=format,
                    )
                else:
                    content, metadata = await self._download_for_schedule(
                        validated_url, sanitized_headers, format
                    )
                    processed = await self._process_content(
                        url=validated_url,
                        content=content,
                        metadata=metadata,
                        format=format,
                    )

                completed_at = datetime.now(UTC)
                size = (
                    len(processed.encode("utf-8")) if isinstance(processed, str) else len(processed)
                )
                return (
                    ScheduleExecution(
                        execution_id=execution_id,
                        schedule_id=schedule_id,
                        status=ExecutionStatus.COMPLETED,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration=(completed_at - started_at).total_seconds(),
                        success=True,
                        content_size=size,
                        error_message=None,
                        attempt=attempt,
                    ),
                    False,
                )
            except Exception as exc:  # noqa: BLE001
                # Record failed execution attempts and classify retryability uniformly.
                completed_at = datetime.now(UTC)
                classified = classify_domain_error(exc, context="schedule")
                logger.warning(
                    f"[SCHEDULE-{schedule_id}] Attempt {attempt} failed for "
                    f"{redact_url_for_log(url)}: {type(exc).__name__}: {exc}"
                )
                return (
                    ScheduleExecution(
                        execution_id=execution_id,
                        schedule_id=schedule_id,
                        status=ExecutionStatus.FAILED,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration=(completed_at - started_at).total_seconds(),
                        success=False,
                        content_size=None,
                        error_message=f"{type(exc).__name__}: {exc}",
                        attempt=attempt,
                    ),
                    classified.retryable,
                )

    async def _process_content(
        self,
        url: str,
        content: bytes,
        metadata: ResponseMetadata | Mapping[str, Any] | None,
        format: str,
    ) -> str | bytes:
        """Delegate scheduled output conversion to the shared `convert()` pipeline."""
        if format not in {"text", "markdown", "html", "pdf", "screenshot", "json", "raw"}:
            logger.warning(f"Unknown format '{format}', returning raw content")
            return content

        if format in {"pdf", "screenshot"}:
            res = await convert(
                ConversionRequest(url=url, output_format=cast(OutputFormat, format)),
                ConversionDeps(renderer=self._resolve_renderer(require_semaphore=True)),
            )
            return res.body

        renderer = self._resolve_renderer(require_semaphore=False)
        normalized_meta = _coerce_metadata(url, content, metadata)
        res = await convert(
            ConversionRequest(
                url=url,
                output_format=cast(OutputFormat, format),
                content=content,
                metadata=normalized_meta,
            ),
            ConversionDeps(renderer=renderer),
        )
        if format == "raw":
            return res.body
        return res.text

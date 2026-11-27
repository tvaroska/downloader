"""Playwright local and split-worker remote adapters implementing `BrowserRenderer`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .. import content_converter as _cc
from .. import pdf_generator as _pg
from ..config import get_settings
from ..models.browser_actions import BrowserAction, BrowserActionError
from ..ports import (
    BrowserCapacityError,
    BrowserPoolError,
    BrowserTaskRequest,
    BrowserTaskResponse,
    ContentConversionError,
    ContentTooLargeError,
    CrawlDelayExceededError,
    PDFGeneratorError,
    RenderTimeoutError,
    RobotsDisallowedError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
    WorkQueue,
    enforce_rendered_output_size,
)
from ..telemetry import get_tracer
from ..validation import (
    DNSResolutionError,
    EgressPolicy,
    SSRFProtectionError,
    URLValidationError,
)
from .session import (
    BrowserSession,
    NavigationOptions,
    RedirectPolicyHook,
    navigate_and_validate,
    run_pre_capture_steps,
)

logger = logging.getLogger(__name__)


async def convert_content_with_playwright_fallback(  # noqa: PLR0913
    url: str,
    output_format: Literal["text", "markdown"] = "text",
    *,
    generator: Any = None,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: RedirectPolicyHook | None = None,
    max_output_bytes: int | None = None,
) -> str:
    """Render `url` via `BrowserSession` and extract text or markdown with `max_download_size` cap."""
    active_gen = generator or _pg.get_shared_pdf_generator()
    cfg = egress_policy.settings if egress_policy is not None else get_settings()
    timeout_ms = cfg.content.html_js_rendering_timeout

    try:
        session = BrowserSession(
            active_gen,
            egress_policy=egress_policy,
            redirect_policy=redirect_policy,
        )
        async with session as (_context, page):
            await navigate_and_validate(
                session,
                url,
                NavigationOptions(
                    wait_until="domcontentloaded",
                    timeout_ms=timeout_ms,
                    settle_timeout_ms=min(timeout_ms, 2500),
                    error_kind="html",
                ),
            )
            await run_pre_capture_steps(
                session,
                url,
                timeout_ms=timeout_ms,
            )
            html_content: str = await page.content()
            html_bytes = html_content.encode("utf-8")
            enforce_rendered_output_size(
                html_bytes,
                max_output_bytes=max_output_bytes,
                egress_policy=egress_policy,
            )
            return await _cc.run_in_html_executor(
                _cc.convert_content,
                html_bytes,
                "text/html; charset=utf-8",
                output_format,
            )
    except (
        URLValidationError,
        RobotsDisallowedError,
        CrawlDelayExceededError,
        ContentTooLargeError,
        ContentConversionError,
        BrowserCapacityError,
        PDFGeneratorError,
    ):
        raise
    except (PlaywrightTimeoutError, TimeoutError) as exc:
        logger.debug(f"Playwright {output_format} fallback timed out for {url}: {exc}")
        raise RenderTimeoutError(f"Playwright {output_format} render timed out: {exc}") from exc
    except (PlaywrightError, BrowserPoolError, RuntimeError, OSError) as exc:
        logger.debug(f"Playwright {output_format} fallback failed for {url}: {exc}")
        raise PDFGeneratorError(f"Playwright {output_format} render failed: {exc}") from exc


async def render_html_with_playwright(  # noqa: PLR0913
    url: str,
    wait_for_selector: str | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    generator: Any = None,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: RedirectPolicyHook | None = None,
    max_output_bytes: int | None = None,
) -> bytes:
    """Render `url` via `BrowserSession` and return UTF-8 HTML bytes within `max_download_size`."""
    active_gen = generator or _pg.get_shared_pdf_generator()
    cfg = egress_policy.settings if egress_policy is not None else get_settings()
    timeout_ms = cfg.content.html_js_rendering_timeout
    tracer = get_tracer(__name__)

    with tracer.start_as_current_span("playwright.render_html") as span:
        span.set_attribute("http.url", url)
        try:
            session = BrowserSession(
                active_gen,
                egress_policy=egress_policy,
                redirect_policy=redirect_policy,
            )
            async with session as (_context, page):
                await navigate_and_validate(
                    session,
                    url,
                    NavigationOptions(
                        wait_until="domcontentloaded",
                        timeout_ms=timeout_ms,
                        settle_timeout_ms=min(timeout_ms, 2500),
                        error_kind="html",
                        include_status_in_error=True,
                    ),
                )
                await run_pre_capture_steps(
                    session,
                    url,
                    wait_for_selector=wait_for_selector,
                    actions=actions,
                    timeout_ms=timeout_ms,
                )
                html_content: str = await page.content()
                html_bytes = html_content.encode("utf-8")
                enforce_rendered_output_size(
                    html_bytes,
                    max_output_bytes=max_output_bytes,
                    egress_policy=egress_policy,
                )
                span.set_attribute("html.bytes", len(html_bytes))
                logger.info(f"Rendered HTML extracted ({len(html_bytes)} bytes) from {url}")
                return html_bytes
        except (
            SelectorTimeoutError,
            BrowserActionError,
            BrowserCapacityError,
            URLValidationError,
            RobotsDisallowedError,
            CrawlDelayExceededError,
            ContentTooLargeError,
            PDFGeneratorError,
        ):
            raise
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            logger.debug(f"Playwright HTML render timed out for {url}: {exc}")
            raise RenderTimeoutError(f"Playwright HTML render timed out: {exc}") from exc
        except (PlaywrightError, BrowserPoolError, RuntimeError, OSError) as exc:
            logger.debug(f"Playwright HTML rendering failed for {url}: {exc}")
            raise PDFGeneratorError(f"Playwright HTML rendering failed: {exc}") from exc


class LocalBrowserRenderer:
    """In-process Playwright adapter enforcing the single `pdf_semaphore` capacity gate."""

    def __init__(
        self,
        pdf_semaphore: asyncio.Semaphore | None = None,
        *,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self.pdf_semaphore = pdf_semaphore
        self.egress_policy = egress_policy
        self.redirect_policy = redirect_policy
        self.max_output_bytes = max_output_bytes

    @asynccontextmanager
    async def _acquire_slot(self, url: str, *, fail_fast: bool) -> AsyncIterator[bool]:
        if self.pdf_semaphore is None:
            yield True
            return
        if self.pdf_semaphore.locked():
            if fail_fast:
                logger.warning(f"Browser rendering at capacity, rejecting request for: {url}")
                raise BrowserCapacityError(
                    "Browser rendering service temporarily unavailable. Please try again later."
                )
            logger.info(f"Browser semaphore saturated, skipping auto-render fallback for {url}")
            yield False
            return
        async with self.pdf_semaphore:
            yield True

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        fail_fast: bool = True,
    ) -> bytes | None:
        async with self._acquire_slot(url, fail_fast=fail_fast) as acquired:
            if not acquired:
                return None
            kwargs: dict[str, Any] = {}
            if actions:
                kwargs["actions"] = actions
            if self.egress_policy is not None:
                kwargs["egress_policy"] = self.egress_policy
            if self.redirect_policy is not None:
                kwargs["redirect_policy"] = self.redirect_policy
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            try:
                return await render_html_with_playwright(url, wait_for_selector, **kwargs)
            except (BrowserCapacityError, BrowserPoolError) as exc:
                if fail_fast:
                    raise
                logger.warning(f"Browser pool unavailable for auto-render of {url}: {exc}")
                return None

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None:
        async with self._acquire_slot(url, fail_fast=False) as acquired:
            if not acquired:
                return None
            kwargs: dict[str, Any] = {}
            if self.egress_policy is not None:
                kwargs["egress_policy"] = self.egress_policy
            if self.redirect_policy is not None:
                kwargs["redirect_policy"] = self.redirect_policy
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            try:
                return await convert_content_with_playwright_fallback(
                    url,
                    output_format,
                    **kwargs,
                )
            except (BrowserCapacityError, BrowserPoolError) as exc:
                logger.warning(f"Browser pool unavailable for auto-fallback of {url}: {exc}")
                return None

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        async with self._acquire_slot(url, fail_fast=True):
            kwargs: dict[str, Any] = {}
            if actions:
                kwargs["actions"] = actions
            if wait_for_selector:
                kwargs["wait_for_selector"] = wait_for_selector
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _pg.generate_pdf_from_url(url, **kwargs)

    async def render_screenshot(
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        async with self._acquire_slot(url, fail_fast=True):
            options: dict[str, Any] = {"type": screenshot_format, "full_page": full_page}
            kwargs: dict[str, Any] = {
                "actions": actions,
                "wait_for_selector": wait_for_selector,
            }
            if self.max_output_bytes is not None:
                kwargs["max_output_bytes"] = self.max_output_bytes
            return await _pg.generate_screenshot_from_url(url, options, **kwargs)


_SIMPLE_REMOTE_ERROR_MAP: dict[str, type[Exception]] = {
    "dns_resolution_error": DNSResolutionError,
    "ssrf_protection_error": SSRFProtectionError,
    "validation_error": URLValidationError,
    "browser_action_error": BrowserActionError,
    "content_too_large": ContentTooLargeError,
    "conversion_error": ContentConversionError,
    "service_unavailable": BrowserCapacityError,
    "timeout_error": RenderTimeoutError,
}


def _rebuild_remote_error(
    req: BrowserTaskRequest,
    resp: BrowserTaskResponse,
    rpc_timeout: float,
) -> Exception:
    err_type = resp.error_type or ""
    msg = resp.error or f"Remote worker browser task ({req.kind}) failed"
    simple_cls = _SIMPLE_REMOTE_ERROR_MAP.get(err_type)
    if simple_cls is not None:
        return simple_cls(msg)
    if err_type == "robots_disallowed":
        return RobotsDisallowedError(req.url, reason=msg)
    if err_type == "crawl_delay_exceeded":
        retry_after = float(resp.timeout_ms) / 1000.0 if resp.timeout_ms else 1.0
        return CrawlDelayExceededError(req.url, retry_after)
    if err_type == "selector_timeout_error":
        timeout_ms = resp.timeout_ms if resp.timeout_ms is not None else int(rpc_timeout * 1000)
        return SelectorTimeoutError(req.wait_for_selector or "", timeout_ms, message=resp.error)
    if err_type == "screenshot_generation_error":
        return ScreenshotGeneratorError(msg, status_code=None)
    status = resp.status_code if (resp.status_code and 400 <= resp.status_code < 600) else None
    if err_type in {"pdf_generation_error", "render_error"} and status == 500:
        status = None
    return PDFGeneratorError(msg, status_code=status)


class RemoteBrowserRenderer:
    """Split-worker RPC adapter implementing `BrowserRenderer` over a `WorkQueue`."""

    def __init__(
        self,
        work_queue: WorkQueue,
        *,
        timeout: float = 45.0,
        egress_policy: EgressPolicy | None = None,
        max_output_bytes: int | None = None,
    ) -> None:
        self.work_queue = work_queue
        self.timeout = timeout
        self.egress_policy = egress_policy
        self.max_output_bytes = max_output_bytes

    async def _dispatch_task(self, req: BrowserTaskRequest) -> bytes:
        resp = await self.work_queue.enqueue_browser_task(req, timeout=self.timeout)
        if not resp.success:
            raise _rebuild_remote_error(req, resp, self.timeout)
        payload = resp.decode_bytes()
        return enforce_rendered_output_size(
            payload,
            max_output_bytes=self.max_output_bytes,
            egress_policy=self.egress_policy,
        )

    async def render_html(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
        fail_fast: bool = True,
    ) -> bytes | None:
        try:
            return await self._dispatch_task(
                BrowserTaskRequest(
                    kind="render_html",
                    url=url,
                    wait_for_selector=wait_for_selector,
                    actions=[a.model_dump() for a in (actions or [])],
                )
            )
        except BrowserCapacityError:
            if fail_fast:
                raise
            logger.info(f"Remote browser queue saturated, skipping auto-render for {url}")
            return None

    async def convert_fallback(
        self,
        url: str,
        output_format: Literal["text", "markdown"],
    ) -> str | None:
        rendered = await self.render_html(url, fail_fast=False)
        if rendered is None:
            return None
        return await _cc.run_in_html_executor(
            _cc.convert_content,
            rendered,
            "text/html; charset=utf-8",
            output_format,
        )

    async def render_pdf(
        self,
        url: str,
        *,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        return await self._dispatch_task(
            BrowserTaskRequest(
                kind="pdf",
                url=url,
                wait_for_selector=wait_for_selector,
                actions=[a.model_dump() for a in (actions or [])],
            )
        )

    async def render_screenshot(
        self,
        url: str,
        *,
        screenshot_format: Literal["png", "jpeg"] = "png",
        full_page: bool = False,
        wait_for_selector: str | None = None,
        actions: Sequence[BrowserAction] | None = None,
    ) -> bytes:
        return await self._dispatch_task(
            BrowserTaskRequest(
                kind="screenshot",
                url=url,
                wait_for_selector=wait_for_selector,
                actions=[a.model_dump() for a in (actions or [])],
                full_page=full_page,
                screenshot_format=screenshot_format,
            )
        )

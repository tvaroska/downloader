"""BrowserSession lifecycle, SSRF route guard, modal closer, and PDF/screenshot capture."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .browser import BrowserConfig, BrowserPool, BrowserPoolError
from .browser.actions import BrowserAction, BrowserActionError, execute_browser_actions
from .config import get_settings
from .http_client import ContentTooLargeError
from .telemetry import get_tracer
from .validation import (
    EgressPolicy,
    SSRFProtectionError,
    URLValidationError,
    sanitize_user_agent,
    validate_url_async,
)

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_ALLOWED_INLINE_SCHEMES = frozenset({"data", "blob", "about"})
_MODAL_CLOSE_SELECTORS: tuple[str, ...] = (
    '[aria-label="close"]',
    '[title="Close"]',
    '[aria-label="Close"]',
    '[title="close"]',
    ".modal-close",
    ".close-button",
)
_MODAL_MAX_CLICKS = 5
_MODAL_TOTAL_BUDGET_SECONDS = 3.0
_DEFAULT_SESSION_TIMEOUT_SECONDS = 60.0


async def _maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


def resolve_browser_user_agent(explicit_user_agent: str | None = None) -> str:
    """Resolve effective User-Agent for Playwright contexts."""
    if isinstance(explicit_user_agent, str) and explicit_user_agent.strip():
        return sanitize_user_agent(explicit_user_agent.strip())

    settings = get_settings()
    crawling = getattr(settings, "crawling", None)
    if crawling is not None:
        fields_set: set[str] = getattr(crawling, "model_fields_set", set())
        configured_ua = getattr(crawling, "user_agent", None)
        if (
            isinstance(configured_ua, str)
            and configured_ua.strip()
            and (
                "user_agent" in fields_set
                or "USER_AGENT" in os.environ
                or configured_ua.strip() != "REST-API-Downloader/1.0"
            )
        ):
            return sanitize_user_agent(configured_ua.strip())

    return DEFAULT_BROWSER_USER_AGENT


def _make_ssrf_route_guard(egress_policy: EgressPolicy | None):
    async def _guard(route: Any) -> None:
        req_url = getattr(getattr(route, "request", None), "url", "") or ""
        parsed = urlparse(req_url)
        scheme = parsed.scheme.lower()

        if scheme in _ALLOWED_INLINE_SCHEMES:
            await _maybe_await(route.continue_())
            return

        if scheme not in ("http", "https"):
            logger.warning(f"Blocked browser request with disallowed scheme '{scheme}': {req_url}")
            await _maybe_await(route.abort("accessdenied"))
            return

        try:
            if egress_policy is not None:
                await egress_policy.validate_url(req_url)
            else:
                await validate_url_async(req_url, get_settings())
        except URLValidationError as exc:
            logger.warning(f"Blocked browser request failing SSRF validation ({req_url}): {exc}")
            await _maybe_await(route.abort("accessdenied"))
            return

        await _maybe_await(route.continue_())

    return _guard


async def _ssrf_route_guard(route: Any) -> None:
    """Block SSRF and non-HTTP(S) subrequests inside Playwright browser contexts."""
    await _make_ssrf_route_guard(None)(route)


def _collect_redirect_chain_urls(request_obj: Any) -> list[str]:
    """Walk `request.redirected_from` backwards and return all string hop URLs oldest-first."""
    hops: list[str] = []
    seen_ids: set[int] = set()
    curr = request_obj
    while curr is not None and id(curr) not in seen_ids and len(hops) < 32:
        seen_ids.add(id(curr))
        hop_url = getattr(curr, "url", None)
        if not isinstance(hop_url, str) or not hop_url:
            break
        hops.append(hop_url)
        curr = getattr(curr, "redirected_from", None)
    hops.reverse()
    return hops


async def _validate_browser_url_candidate(
    candidate_url: str,
    egress_policy: EgressPolicy | None = None,
) -> None:
    """Validate a single browser navigation or redirect URL against scheme and SSRF policy."""
    parsed = urlparse(candidate_url)
    scheme = parsed.scheme.lower()
    if scheme in _ALLOWED_INLINE_SCHEMES:
        return
    if scheme not in ("http", "https"):
        raise SSRFProtectionError(
            f"Browser navigated to disallowed scheme '{scheme}': {candidate_url}"
        )
    if egress_policy is not None:
        await egress_policy.validate_url(candidate_url)
    else:
        await validate_url_async(candidate_url, get_settings())


def _register_redirect_listener(
    context: Any,
    egress_policy: EgressPolicy | None,
) -> tuple[Any, Any] | None:
    on_fn = getattr(context, "on", None)
    if not callable(on_fn):
        return None
    violations: list[URLValidationError] = []
    pending_checks: set[asyncio.Task[None]] = set()
    try:
        context._ssrf_redirect_violations = violations
        context._ssrf_redirect_tasks = pending_checks
    except (AttributeError, TypeError):
        return None

    async def _check_redirected_request(req: Any) -> None:
        for hop_url in _collect_redirect_chain_urls(req):
            try:
                await _validate_browser_url_candidate(hop_url, egress_policy)
            except URLValidationError as exc:
                logger.warning(f"Blocked redirected browser request ({hop_url}): {exc}")
                violations.append(exc)
                return

    def _on_request(req: Any) -> None:
        prev = getattr(req, "redirected_from", None)
        if prev is None or not isinstance(getattr(req, "url", None), str):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(_check_redirected_request(req))
        pending_checks.add(task)
        task.add_done_callback(pending_checks.discard)

    return on_fn, _on_request


async def install_ssrf_route_guard(
    context: Any,
    *,
    egress_policy: EgressPolicy | None = None,
) -> None:
    """Register SSRF route interception and subresource redirect tracking on a BrowserContext."""
    route_fn = getattr(context, "route", None)
    if not callable(route_fn):
        raise TypeError("BrowserContext must expose a callable route() method for SSRF protection")

    guard = _ssrf_route_guard if egress_policy is None else _make_ssrf_route_guard(egress_policy)
    await _maybe_await(route_fn("**/*", guard))

    listener = _register_redirect_listener(context, egress_policy)
    if listener is not None:
        on_fn, on_request = listener
        try:
            await _maybe_await(on_fn("request", on_request))
        except (PlaywrightError, RuntimeError, TypeError) as exc:
            logger.debug(f"Could not attach redirect listener on browser context: {exc}")


async def validate_browser_navigation(
    page: Any,
    response: Any = None,
    context: Any = None,
    *,
    egress_policy: EgressPolicy | None = None,
) -> None:
    """Re-validate every redirect hop in `response.request.redirected_from` and final `page.url`."""
    if context is not None:
        pending = getattr(context, "_ssrf_redirect_tasks", None)
        if isinstance(pending, set) and pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
        recorded = getattr(context, "_ssrf_redirect_violations", None)
        if isinstance(recorded, list) and recorded:
            raise recorded[0]

    candidates: list[str] = []
    if response is not None:
        candidates.extend(_collect_redirect_chain_urls(getattr(response, "request", None)))
        resp_url = getattr(response, "url", None)
        if isinstance(resp_url, str) and resp_url:
            candidates.append(resp_url)

    if page is not None:
        page_url = getattr(page, "url", None)
        if isinstance(page_url, str) and page_url:
            candidates.append(page_url)

    seen: set[str] = set()
    for url_str in candidates:
        if url_str in seen:
            continue
        seen.add(url_str)
        await _validate_browser_url_candidate(url_str, egress_policy)


async def close_page_modals(
    page: Any,
    *,
    max_clicks: int = _MODAL_MAX_CLICKS,
    timeout_seconds: float = _MODAL_TOTAL_BUDGET_SECONDS,
) -> None:
    """Best-effort, bounded dismissal of common signup/cookie modal overlays."""
    if max_clicks <= 0 or timeout_seconds <= 0:
        return
    clicks = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            for selector in _MODAL_CLOSE_SELECTORS:
                if clicks >= max_clicks:
                    return
                try:
                    close_buttons = await page.query_selector_all(selector)
                except (PlaywrightError, TimeoutError, RuntimeError, OSError) as exc:
                    logger.debug(f"Modal selector lookup skipped for {selector}: {exc}")
                    continue
                for button in close_buttons:
                    if clicks >= max_clicks:
                        return
                    clicks += 1
                    try:
                        await button.click(timeout=1000)
                        logger.debug(f"Closed modal/popup with selector: {selector}")
                        await page.wait_for_timeout(200)
                    except (PlaywrightError, TimeoutError, RuntimeError, OSError) as exc:
                        logger.debug(f"Modal close click ignored for {selector}: {exc}")
    except TimeoutError:
        logger.debug(f"Modal closer reached {timeout_seconds}s budget after {clicks} attempts")


def enforce_rendered_output_size(
    payload: bytes,
    *,
    max_output_bytes: int | None = None,
    egress_policy: EgressPolicy | None = None,
) -> bytes:
    """Reject rendered HTML, PDF, or screenshot outputs exceeding `max_download_size`."""
    cap = (
        max_output_bytes
        if max_output_bytes is not None
        else (
            egress_policy.settings.content.max_download_size
            if egress_policy is not None
            else get_settings().content.max_download_size
        )
    )
    if len(payload) > cap:
        raise ContentTooLargeError(max_size=cap, actual_size=len(payload))
    return payload


class PDFGeneratorError(Exception):
    """Raised when PDF or screenshot generation fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BrowserSession:
    """Single async context manager acquiring an isolated, SSRF-guarded `(context, page)`."""

    def __init__(
        self,
        generator: Any,
        *,
        user_agent: str | None = None,
        egress_policy: EgressPolicy | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._generator = generator
        self._user_agent = user_agent
        self._egress_policy = egress_policy
        self._timeout_seconds = timeout_seconds
        self._timeout_cm: asyncio.Timeout | None = None
        self._pool: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._acquired_mark = False

    def _resolve_timeout_budget(self) -> float:
        if self._timeout_seconds is not None:
            return self._timeout_seconds
        page_load_ms = getattr(self._generator, "page_load_timeout", None)
        if isinstance(page_load_ms, (int, float)) and page_load_ms > 0:
            return (float(page_load_ms) / 1000.0) * 2.0 + 10.0
        return _DEFAULT_SESSION_TIMEOUT_SECONDS

    async def __aenter__(self) -> tuple[Any, Any]:
        if self._generator is None:
            raise PDFGeneratorError("PDF generator pool not initialized")

        mark_acq = getattr(self._generator, "mark_context_acquired", None)
        if callable(mark_acq) and (
            isinstance(self._generator, PlaywrightPDFGenerator)
            or "mark_context_acquired" in getattr(self._generator, "__dict__", {})
        ):
            mark_acq()
            self._acquired_mark = True

        budget = self._resolve_timeout_budget()
        if budget > 0:
            self._timeout_cm = asyncio.timeout(budget)
            await self._timeout_cm.__aenter__()

        try:
            self._pool = await self._generator.ensure_started()
            if not self._pool:
                raise PDFGeneratorError("PDF generator pool not initialized")

            self.browser = await self._pool.get_browser()
            gen_ua = getattr(self._generator, "user_agent", None)
            effective_ua = resolve_browser_user_agent(
                self._user_agent
                if self._user_agent is not None
                else (gen_ua if isinstance(gen_ua, str) else None)
            )

            if (isinstance(BrowserPool, type) and isinstance(self._pool, BrowserPool)) or (
                "create_context" in getattr(self._pool, "__dict__", {})
                and callable(getattr(self._pool, "create_context", None))
            ):
                self.context = await self._pool.create_context(
                    self.browser, user_agent=effective_ua
                )
            else:
                self.context = await self.browser.new_context(
                    user_agent=effective_ua,
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=False,
                    java_script_enabled=True,
                    bypass_csp=False,
                    service_workers="block",
                    accept_downloads=False,
                )

            await install_ssrf_route_guard(self.context, egress_policy=self._egress_policy)
            self.page = await self.context.new_page()
            return self.context, self.page
        except BaseException as exc:
            if self._timeout_cm is not None:
                cm = self._timeout_cm
                self._timeout_cm = None
                try:
                    await cm.__aexit__(type(exc), exc, exc.__traceback__)
                finally:
                    await self._cleanup()
            else:
                await self._cleanup()
            raise

    async def _run_shielded_cleanup_step(
        self,
        awaitable: Any,
        error_label: str,
    ) -> asyncio.CancelledError | None:
        try:
            await asyncio.shield(awaitable)
        except asyncio.CancelledError as exc:
            return exc
        except (PlaywrightError, RuntimeError, OSError) as exc:
            logger.warning(f"{error_label}: {exc}")
        return None

    async def _cleanup(self) -> None:
        cancelled: asyncio.CancelledError | None = None
        try:
            if self.context is not None:
                ctx = self.context
                self.context = None
                cancelled = await self._run_shielded_cleanup_step(
                    ctx.close(), "Error closing browser context"
                )

            gen_pool = (
                self._generator.pool
                if isinstance(self._generator, PlaywrightPDFGenerator)
                else None
            )
            active_pool = gen_pool or self._pool
            if self.browser is not None and active_pool is not None:
                br = self.browser
                self.browser = None
                rel_cancelled = await self._run_shielded_cleanup_step(
                    active_pool.release_browser(br), "Error releasing browser to pool"
                )
                if cancelled is None:
                    cancelled = rel_cancelled
        finally:
            if self._acquired_mark:
                self._acquired_mark = False
                mark_rel = getattr(self._generator, "mark_context_released", None)
                if callable(mark_rel):
                    mark_rel()

        if cancelled is not None:
            raise cancelled

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if self._timeout_cm is not None:
                cm = self._timeout_cm
                self._timeout_cm = None
                await cm.__aexit__(exc_type, exc_val, exc_tb)
        finally:
            await self._cleanup()


class PlaywrightPDFGenerator:
    """PDF and screenshot generator backed by a lazy, idle-reaped `BrowserPool`."""

    def __init__(  # noqa: PLR0913
        self,
        pool_size: int = 3,
        page_load_timeout: int = 30000,
        wait_until: str = "networkidle",
        memory_limit_mb: int = 512,
        max_uses_per_browser: int = 50,
        idle_timeout: float = 600.0,
        user_agent: str | None = None,
        *,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self.pool: BrowserPool | None = None
        self._start_lock = asyncio.Lock()
        self.pool_config = BrowserConfig(
            pool_size=pool_size,
            memory_limit_mb=memory_limit_mb,
            max_uses_per_browser=max_uses_per_browser,
        )
        self.page_load_timeout = page_load_timeout
        self.wait_until = wait_until
        self.idle_timeout = idle_timeout
        self.user_agent = user_agent
        self._egress_policy = egress_policy
        self._active_contexts: int = 0
        self._last_used_at: float = 0.0
        self._idle_task: asyncio.Task[None] | None = None

    @property
    def pool_size(self) -> int:
        """Return configured browser pool size."""
        return self.pool_config.pool_size

    def mark_context_acquired(self) -> None:
        """Record an active browser context and update last-used timestamp."""
        self._active_contexts += 1
        self._last_used_at = time.monotonic()

    def mark_context_released(self) -> None:
        """Record browser context release and update last-used timestamp."""
        self._active_contexts = max(0, self._active_contexts - 1)
        self._last_used_at = time.monotonic()

    async def __aenter__(self) -> PlaywrightPDFGenerator:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _cancel_idle_task(self) -> None:
        """Cancel and await the background idle reaper task if running."""
        if self._idle_task is not None:
            if not self._idle_task.done() and self._idle_task is not asyncio.current_task():
                self._idle_task.cancel()
                try:
                    await self._idle_task
                except asyncio.CancelledError:
                    pass
            self._idle_task = None

    async def _idle_reaper_loop(self) -> None:
        """Close the browser pool after `idle_timeout` with zero active contexts."""
        check_interval = max(min(self.idle_timeout / 2.0, 30.0), 0.05)
        try:
            while True:
                await asyncio.sleep(check_interval)
                if self.pool is None:
                    return
                if (
                    self._active_contexts == 0
                    and (time.monotonic() - self._last_used_at) >= self.idle_timeout
                ):
                    async with self._start_lock:
                        if (
                            self._active_contexts == 0
                            and self.pool is not None
                            and (time.monotonic() - self._last_used_at) >= self.idle_timeout
                        ):
                            pool_to_close = self.pool
                            self.pool = None
                            await pool_to_close.close()
                            logger.info("Browser pool closed after idle timeout")
                            return
        except asyncio.CancelledError:
            return

    async def start(self) -> None:
        """Initialize browser pool."""
        await self._cancel_idle_task()
        try:
            if self._egress_policy is not None:
                pool = BrowserPool(config=self.pool_config, egress_policy=self._egress_policy)
            else:
                pool = BrowserPool(config=self.pool_config)
            await pool.start()
            self.pool = pool
            self._last_used_at = time.monotonic()
            if self.idle_timeout > 0:
                self._idle_task = asyncio.create_task(self._idle_reaper_loop())
            logger.info("PDF generator initialized with browser pool")
        except BrowserPoolError as exc:
            self.pool = None
            logger.error(f"Failed to initialize PDF generator: {exc}")
            raise PDFGeneratorError(f"PDF generator initialization failed: {exc}") from exc

    async def ensure_started(self) -> BrowserPool:
        """Lazily initialize browser pool on first use under an async lock."""
        self._last_used_at = time.monotonic()
        if self.pool is not None:
            return self.pool
        async with self._start_lock:
            if self.pool is None:
                await self.start()
            else:
                self._last_used_at = time.monotonic()
        assert self.pool is not None
        return self.pool

    async def close(self) -> None:
        """Close browser pool and cleanup."""
        try:
            await self._cancel_idle_task()
            if self.pool:
                await self.pool.close()
                self.pool = None
            logger.info("PDF generator closed")
        except (PlaywrightError, BrowserPoolError, RuntimeError, OSError) as exc:
            logger.error(f"Error closing PDF generator: {exc}")

    @asynccontextmanager
    async def _get_browser_context(self) -> AsyncIterator[tuple[Any, Any]]:
        """Delegate context acquisition and teardown to the shared `BrowserSession`."""
        session = BrowserSession(self, egress_policy=self._egress_policy)
        async with session as (context, _page):
            yield session.browser, context

    async def _navigate_for_capture(
        self,
        session: BrowserSession,
        url: str,
        *,
        wait_until: Any,
        timeouts_ms: tuple[int, int],
    ) -> None:
        page = session.page
        timeout_ms, settle_timeout_ms = timeouts_ms
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            raise PDFGeneratorError(f"Timeout loading page: {url}") from exc

        if not response:
            raise PDFGeneratorError(f"Failed to load page: {url}")
        if response.status >= 400:
            raise PDFGeneratorError(
                f"HTTP {response.status}: {response.status_text}",
                status_code=response.status,
            )

        await validate_browser_navigation(
            page, response, session.context, egress_policy=self._egress_policy
        )
        if wait_until != "networkidle":
            try:
                await page.wait_for_load_state("networkidle", timeout=settle_timeout_ms)
            except (PlaywrightTimeoutError, TimeoutError):
                logger.debug(f"Proceeding after {settle_timeout_ms}ms settle: {url}")

    async def _run_pre_capture_steps(
        self,
        page: Any,
        context: Any,
        *,
        wait_for_selector: str | None,
        actions: Sequence[BrowserAction] | None,
        timeout_ms: int,
    ) -> None:
        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=timeout_ms, state="visible")
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                from .content_converter import SelectorTimeoutError

                logger.warning(f"Selector '{wait_for_selector}' not found within {timeout_ms}ms")
                raise SelectorTimeoutError(wait_for_selector, timeout_ms) from exc
        if actions:
            await execute_browser_actions(page, actions, default_timeout_ms=timeout_ms)
        else:
            await close_page_modals(page)
        await validate_browser_navigation(page, None, context, egress_policy=self._egress_policy)

    async def generate_pdf(
        self,
        url: str,
        options: dict[str, Any] | None = None,
        *,
        actions: Sequence[BrowserAction] | None = None,
        wait_for_selector: str | None = None,
        max_output_bytes: int | None = None,
    ) -> bytes:
        """Generate PDF from URL with `BrowserSession` isolation and `max_download_size` check."""
        pdf_options: dict[str, Any] = {
            "format": "A4",
            "print_background": True,
            "margin": {"top": "20px", "right": "20px", "bottom": "20px", "left": "20px"},
            "wait_for": self.wait_until,
            "timeout": self.page_load_timeout,
        }
        if options:
            pdf_options.update(options)

        timeout_ms = int(pdf_options.get("timeout", 30000))
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("playwright.generate_pdf") as span:
            span.set_attribute("http.url", url)
            try:
                session = BrowserSession(self, egress_policy=self._egress_policy)
                async with session as (context, page):
                    logger.info(f"Loading page: {url}")
                    await self._navigate_for_capture(
                        session,
                        url,
                        wait_until=pdf_options.get("wait_for", "networkidle"),
                        timeouts_ms=(timeout_ms, min(timeout_ms, 2500)),
                    )
                    await self._run_pre_capture_steps(
                        page,
                        context,
                        wait_for_selector=wait_for_selector,
                        actions=actions,
                        timeout_ms=timeout_ms,
                    )
                    pdf_bytes: bytes = await page.pdf(
                        format=pdf_options.get("format", "A4"),
                        print_background=pdf_options.get("print_background", True),
                        margin=pdf_options["margin"],
                        prefer_css_page_size=True,
                        display_header_footer=False,
                    )
                    enforce_rendered_output_size(
                        pdf_bytes,
                        max_output_bytes=max_output_bytes,
                        egress_policy=self._egress_policy,
                    )
                    span.set_attribute("pdf.bytes", len(pdf_bytes))
                    logger.info(f"PDF generated successfully, size: {len(pdf_bytes)} bytes")
                    return pdf_bytes
            except (
                PDFGeneratorError,
                BrowserActionError,
                URLValidationError,
                ContentTooLargeError,
            ):
                raise
            except (PlaywrightError, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                logger.error(f"Unexpected error generating PDF: {exc}")
                raise PDFGeneratorError(f"PDF generation failed: {exc}") from exc

    async def generate_screenshot(
        self,
        url: str,
        options: dict[str, Any] | None = None,
        *,
        actions: Sequence[BrowserAction] | None = None,
        wait_for_selector: str | None = None,
        max_output_bytes: int | None = None,
    ) -> bytes:
        """Capture a PNG or JPEG screenshot from a URL with `max_download_size` enforcement."""
        shot_options: dict[str, Any] = {
            "type": "png",
            "full_page": False,
            "timeout": self.page_load_timeout,
        }
        if options:
            shot_options.update(options)

        image_type = shot_options.get("type", "png")
        if image_type not in ("png", "jpeg"):
            raise PDFGeneratorError(f"Unsupported screenshot format: {image_type}")

        timeout_ms = int(shot_options.get("timeout", self.page_load_timeout))
        tracer = get_tracer(__name__)
        with tracer.start_as_current_span("playwright.screenshot") as span:
            span.set_attribute("http.url", url)
            span.set_attribute("screenshot.type", image_type)
            span.set_attribute("screenshot.full_page", bool(shot_options.get("full_page", False)))
            try:
                session = BrowserSession(self, egress_policy=self._egress_policy)
                async with session as (context, page):
                    await self._navigate_for_capture(
                        session,
                        url,
                        wait_until="domcontentloaded",
                        timeouts_ms=(timeout_ms, min(timeout_ms, 2500)),
                    )
                    await self._run_pre_capture_steps(
                        page,
                        context,
                        wait_for_selector=wait_for_selector,
                        actions=actions,
                        timeout_ms=timeout_ms,
                    )
                    screenshot_kwargs: dict[str, Any] = {
                        "type": image_type,
                        "full_page": bool(shot_options.get("full_page", False)),
                    }
                    if image_type == "jpeg" and "quality" in shot_options:
                        screenshot_kwargs["quality"] = int(shot_options["quality"])

                    screenshot_bytes: bytes = await page.screenshot(**screenshot_kwargs)
                    enforce_rendered_output_size(
                        screenshot_bytes,
                        max_output_bytes=max_output_bytes,
                        egress_policy=self._egress_policy,
                    )
                    span.set_attribute("screenshot.bytes", len(screenshot_bytes))
                    logger.info(
                        f"Screenshot captured ({image_type}), size: {len(screenshot_bytes)} bytes"
                    )
                    return screenshot_bytes
            except (
                PDFGeneratorError,
                BrowserActionError,
                URLValidationError,
                ContentTooLargeError,
            ):
                raise
            except (PlaywrightError, TimeoutError, RuntimeError, OSError, ValueError) as exc:
                logger.error(f"Unexpected error capturing screenshot: {exc}")
                raise PDFGeneratorError(f"Screenshot generation failed: {exc}") from exc


_pdf_generator: PlaywrightPDFGenerator | None = None
_initialization_lock = asyncio.Lock()


@asynccontextmanager
async def get_pdf_generator() -> AsyncIterator[PlaywrightPDFGenerator]:
    """Get or create the shared `PlaywrightPDFGenerator` instance."""
    global _pdf_generator

    async with _initialization_lock:
        if _pdf_generator is None:
            cfg = get_settings()
            generator = PlaywrightPDFGenerator(
                pool_size=cfg.pdf.pool_size,
                page_load_timeout=cfg.pdf.page_load_timeout,
                wait_until=cfg.pdf.wait_until,
                memory_limit_mb=cfg.pdf.browser_memory_limit_mb,
                max_uses_per_browser=cfg.pdf.max_uses_per_browser,
                idle_timeout=cfg.pdf.idle_timeout,
                user_agent=None,
            )
            await generator.start()
            _pdf_generator = generator

    try:
        assert _pdf_generator is not None
        yield _pdf_generator
    except (PDFGeneratorError, BrowserPoolError, PlaywrightError, RuntimeError, OSError) as exc:
        logger.error(f"Error in PDF generator: {exc}")
        if "initialization failed" in str(exc).lower():
            async with _initialization_lock:
                if _pdf_generator:
                    await _pdf_generator.close()
                    _pdf_generator = None
        raise


async def generate_pdf_from_url(
    url: str,
    options: dict[str, Any] | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    wait_for_selector: str | None = None,
    max_output_bytes: int | None = None,
) -> bytes:
    """Generate PDF from URL using shared generator instance."""
    async with get_pdf_generator() as generator:
        if actions or wait_for_selector or max_output_bytes is not None:
            return await generator.generate_pdf(
                url,
                options,
                actions=actions,
                wait_for_selector=wait_for_selector,
                max_output_bytes=max_output_bytes,
            )
        return await generator.generate_pdf(url, options)


async def generate_screenshot_from_url(
    url: str,
    options: dict[str, Any] | None = None,
    *,
    actions: Sequence[BrowserAction] | None = None,
    wait_for_selector: str | None = None,
    max_output_bytes: int | None = None,
) -> bytes:
    """Capture a screenshot from URL using shared generator instance."""
    async with get_pdf_generator() as generator:
        if max_output_bytes is not None:
            return await generator.generate_screenshot(
                url,
                options,
                actions=actions,
                wait_for_selector=wait_for_selector,
                max_output_bytes=max_output_bytes,
            )
        return await generator.generate_screenshot(
            url,
            options,
            actions=actions,
            wait_for_selector=wait_for_selector,
        )


def set_shared_pdf_generator(generator: PlaywrightPDFGenerator | None) -> None:
    """Register or clear the shared PDF generator instance."""
    global _pdf_generator
    _pdf_generator = generator


def get_shared_pdf_generator() -> PlaywrightPDFGenerator | None:
    """Get the shared PDF generator instance if it exists."""
    return _pdf_generator


async def cleanup_pdf_generator() -> None:
    """Cleanup global PDF generator instance."""
    global _pdf_generator
    if _pdf_generator:
        await _pdf_generator.close()
        _pdf_generator = None

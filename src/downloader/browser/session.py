"""BrowserSession lifecycle, SSRF route/redirect guards, modal dismissal, and navigation helpers."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..config import get_settings
from ..models.browser_actions import BrowserAction
from ..ports import (
    BrowserAcquireTimeoutError,
    BrowserCapacityError,
    BrowserPoolError,
    PDFGeneratorError,
    RenderTimeoutError,
    ScreenshotGeneratorError,
    SelectorTimeoutError,
)
from ..validation import (
    EgressPolicy,
    SSRFProtectionError,
    URLValidationError,
    canonical_origin,
    sanitize_user_agent,
    validate_url_async,
)
from .actions import execute_browser_actions

logger = logging.getLogger(__name__)

RedirectPolicyHook = Callable[[str, str], Awaitable[None]]

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
_MODAL_CLICK_TIMEOUT_CAP_MS = 1000
_MODAL_SETTLE_DELAY_CAP_MS = 200
_DEFAULT_SESSION_TIMEOUT_SECONDS = 60.0
_NETWORKIDLE_SETTLE_CAP_MS = 2500


async def _maybe_await(value: Any) -> Any:
    """Await a value only when it is awaitable."""
    if inspect.isawaitable(value):
        return await value
    return value


async def _safe_route_reply(route_method: Callable[..., Any], *args: Any) -> None:
    """Invoke a Playwright route continuation or abort without leaking teardown errors."""
    try:
        await _maybe_await(route_method(*args))
    except PlaywrightError as exc:
        logger.debug(f"Browser route callback ignored after context close: {exc}")


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


def _record_navigation_violation(context: Any, request_obj: Any, exc: URLValidationError) -> None:
    if context is None:
        return
    is_nav_fn = getattr(request_obj, "is_navigation_request", None)
    is_nav = bool(is_nav_fn()) if callable(is_nav_fn) else False
    if not is_nav:
        return
    violations = getattr(context, "_ssrf_redirect_violations", None)
    if isinstance(violations, list):
        violations.append(exc)


def _make_ssrf_route_guard(
    egress_policy: EgressPolicy | None,
    *,
    context: Any = None,
) -> Callable[[Any], Awaitable[None]]:
    async def _guard(route: Any) -> None:
        req = getattr(route, "request", None)
        req_url = getattr(req, "url", "") or ""
        parsed = urlparse(req_url)
        scheme = parsed.scheme.lower()

        if scheme in _ALLOWED_INLINE_SCHEMES:
            await _safe_route_reply(route.continue_)
            return

        if scheme not in ("http", "https"):
            logger.warning(f"Blocked browser request with disallowed scheme '{scheme}': {req_url}")
            _record_navigation_violation(
                context,
                req,
                SSRFProtectionError(
                    f"Browser request with disallowed scheme '{scheme}': {req_url}"
                ),
            )
            await _safe_route_reply(route.abort, "accessdenied")
            return

        try:
            if egress_policy is not None:
                await egress_policy.validate_url(req_url)
            else:
                await validate_url_async(req_url, get_settings())
        except URLValidationError as exc:
            logger.warning(f"Blocked browser request failing SSRF validation ({req_url}): {exc}")
            _record_navigation_violation(context, req, exc)
            await _safe_route_reply(route.abort, "accessdenied")
            return

        await _safe_route_reply(route.continue_)

    return _guard


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

    listener = _register_redirect_listener(context, egress_policy)
    guard = _make_ssrf_route_guard(egress_policy, context=context)
    await _maybe_await(route_fn("**/*", guard))

    if listener is not None:
        on_fn, on_request = listener
        try:
            await _maybe_await(on_fn("request", on_request))
        except (PlaywrightError, RuntimeError, TypeError) as exc:
            logger.debug(f"Could not attach redirect listener on browser context: {exc}")


async def flush_context_navigation_violations(context: Any) -> None:
    """Await pending redirect listener checks on `context` and raise any recorded SSRF violation."""
    if context is None:
        return
    pending = getattr(context, "_ssrf_redirect_tasks", None)
    if isinstance(pending, set) and pending:
        await asyncio.gather(*list(pending), return_exceptions=True)
    recorded = getattr(context, "_ssrf_redirect_violations", None)
    if isinstance(recorded, list) and recorded:
        raise recorded[0]


def _collect_navigation_candidates(page: Any, response: Any) -> list[str]:
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
    return list(dict.fromkeys(candidates))


async def _enforce_cross_origin_redirects(
    ordered_urls: list[str],
    initial_url: str | None,
    redirect_policy: RedirectPolicyHook,
) -> None:
    hop_sequence: list[str] = [initial_url] if initial_url else []
    for url_str in ordered_urls:
        if not hop_sequence or hop_sequence[-1] != url_str:
            hop_sequence.append(url_str)

    checked: set[tuple[tuple[str, str, int] | None, str]] = set()
    for prev_url, next_url in zip(hop_sequence, hop_sequence[1:], strict=False):
        prev_origin = canonical_origin(prev_url)
        next_origin = canonical_origin(next_url)
        if prev_origin is None or next_origin is None or prev_origin == next_origin:
            continue
        pair_key = (prev_origin, next_url)
        if pair_key not in checked:
            checked.add(pair_key)
            await redirect_policy(prev_url, next_url)


async def validate_browser_navigation(  # noqa: PLR0913
    page: Any,
    response: Any = None,
    context: Any = None,
    *,
    egress_policy: EgressPolicy | None = None,
    redirect_policy: RedirectPolicyHook | None = None,
    initial_url: str | None = None,
) -> None:
    """Re-validate every redirect hop and final `page.url` against SSRF and cross-origin robots."""
    await flush_context_navigation_violations(context)
    ordered_urls = _collect_navigation_candidates(page, response)
    for url_str in ordered_urls:
        await _validate_browser_url_candidate(url_str, egress_policy)
    if redirect_policy is not None:
        await _enforce_cross_origin_redirects(ordered_urls, initial_url, redirect_policy)


def _remaining_modal_ms(deadline: float) -> int:
    return int((deadline - time.monotonic()) * 1000)


async def _is_modal_button_visible(button: Any) -> bool:
    is_visible_fn = getattr(button, "is_visible", None)
    if not callable(is_visible_fn):
        return True
    return bool(await _maybe_await(is_visible_fn()))


async def _try_dismiss_modal_button(
    button: Any,
    page: Any,
    selector: str,
    deadline: float,
) -> bool:
    """Click one visible modal close button within remaining budget; return True if a click ran."""
    if _remaining_modal_ms(deadline) <= 0:
        return False
    clicked = False
    try:
        if not await _is_modal_button_visible(button):
            return False
        remaining_ms = _remaining_modal_ms(deadline)
        if remaining_ms <= 0:
            return False
        clicked = True
        await button.click(timeout=min(_MODAL_CLICK_TIMEOUT_CAP_MS, remaining_ms))
        logger.debug(f"Closed modal/popup with selector: {selector}")
        settle_ms = min(_MODAL_SETTLE_DELAY_CAP_MS, _remaining_modal_ms(deadline))
        if settle_ms > 0:
            await page.wait_for_timeout(settle_ms)
    except (PlaywrightError, TimeoutError, RuntimeError, OSError) as exc:
        logger.debug(f"Modal close click ignored for {selector}: {exc}")
    return clicked


async def close_page_modals(
    page: Any,
    *,
    max_clicks: int = _MODAL_MAX_CLICKS,
    timeout_seconds: float = _MODAL_TOTAL_BUDGET_SECONDS,
) -> None:
    """Best-effort, bounded dismissal of common signup/cookie modal overlays."""
    if max_clicks <= 0 or timeout_seconds <= 0:
        return
    deadline = time.monotonic() + timeout_seconds
    clicks = 0
    for selector in _MODAL_CLOSE_SELECTORS:
        if clicks >= max_clicks or _remaining_modal_ms(deadline) <= 0:
            break
        try:
            close_buttons = await page.query_selector_all(selector)
        except (PlaywrightError, TimeoutError, RuntimeError, OSError) as exc:
            logger.debug(f"Modal selector lookup skipped for {selector}: {exc}")
            continue
        for button in close_buttons:
            if clicks >= max_clicks or _remaining_modal_ms(deadline) <= 0:
                break
            if await _try_dismiss_modal_button(button, page, selector, deadline):
                clicks += 1
    if _remaining_modal_ms(deadline) <= 0:
        logger.debug(f"Modal closer reached {timeout_seconds}s budget after {clicks} attempts")


@dataclass(frozen=True)
class NavigationOptions:
    """Navigation timing and error formatting parameters for `navigate_and_validate`."""

    wait_until: Any = "domcontentloaded"
    timeout_ms: int = 30_000
    settle_timeout_ms: int = _NETWORKIDLE_SETTLE_CAP_MS
    error_kind: Literal["pdf", "screenshot", "html"] = "pdf"
    include_status_in_error: bool = False


async def navigate_and_validate(
    session: BrowserSession,
    url: str,
    options: NavigationOptions,
) -> None:
    """Navigate with Playwright, validate redirect hops before status checks, and settle DOM."""
    page = session.page
    err_cls: type[PDFGeneratorError] = (
        ScreenshotGeneratorError if options.error_kind == "screenshot" else PDFGeneratorError
    )
    try:
        response = await page.goto(
            url,
            wait_until=options.wait_until,
            timeout=options.timeout_ms,
        )
    except (PlaywrightTimeoutError, TimeoutError) as exc:
        await flush_context_navigation_violations(session.context)
        raise RenderTimeoutError(f"Timeout loading page: {url}") from exc
    except PlaywrightError as exc:
        await flush_context_navigation_violations(session.context)
        raise err_cls(f"Failed to load page: {url}: {exc}") from exc

    await validate_browser_navigation(
        page,
        response,
        session.context,
        egress_policy=session._egress_policy,
        redirect_policy=session._redirect_policy,
        initial_url=url,
    )

    if not response:
        if options.include_status_in_error:
            raise err_cls(f"Failed to load page: {url} (status: no response)")
        raise err_cls(f"Failed to load page: {url}")
    if response.status >= 400:
        if options.error_kind == "html":
            msg = (
                f"Failed to load page: {url} (status: {response.status})"
                if options.include_status_in_error
                else f"Failed to load page: {url}"
            )
            raise PDFGeneratorError(msg, status_code=response.status)
        raise err_cls(
            f"HTTP {response.status}: {response.status_text}",
            status_code=response.status,
        )

    if options.wait_until != "networkidle":
        try:
            await page.wait_for_load_state("networkidle", timeout=options.settle_timeout_ms)
        except (PlaywrightTimeoutError, TimeoutError):
            logger.debug(f"Proceeding after {options.settle_timeout_ms}ms settle: {url}")


async def run_pre_capture_steps(
    session: BrowserSession,
    url: str,
    *,
    wait_for_selector: str | None = None,
    actions: Sequence[BrowserAction] | None = None,
    timeout_ms: int = 30_000,
) -> None:
    """Wait for selector, run caller actions or modal closer, and re-validate navigation."""
    page = session.page
    if wait_for_selector:
        try:
            await page.wait_for_selector(wait_for_selector, timeout=timeout_ms, state="visible")
        except (PlaywrightTimeoutError, TimeoutError) as exc:
            logger.warning(f"Selector '{wait_for_selector}' not found within {timeout_ms}ms")
            raise SelectorTimeoutError(wait_for_selector, timeout_ms) from exc
    if actions:
        await execute_browser_actions(page, actions, default_timeout_ms=timeout_ms)
    else:
        await close_page_modals(page)
    await validate_browser_navigation(
        page,
        None,
        session.context,
        egress_policy=session._egress_policy,
        redirect_policy=session._redirect_policy,
        initial_url=url,
    )


class BrowserSession:
    """Single async context manager acquiring an isolated, SSRF-guarded `(context, page)`."""

    def __init__(
        self,
        generator: Any,
        *,
        user_agent: str | None = None,
        egress_policy: EgressPolicy | None = None,
        redirect_policy: RedirectPolicyHook | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._generator = generator
        self._user_agent = user_agent
        self._egress_policy = egress_policy
        self._redirect_policy = redirect_policy
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
        if callable(mark_acq):
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
            self.context = await self._pool.create_context(self.browser, user_agent=effective_ua)
            await install_ssrf_route_guard(self.context, egress_policy=self._egress_policy)
            self.page = await self.context.new_page()
            return self.context, self.page
        except BrowserAcquireTimeoutError as exc:
            await self._abort_enter(exc)
            raise BrowserCapacityError(str(exc)) from exc
        except BrowserPoolError as exc:
            await self._abort_enter(exc)
            raise PDFGeneratorError(f"Browser pool error: {exc}") from exc
        except BaseException as exc:
            await self._abort_enter(exc)
            raise

    async def _abort_enter(self, exc: BaseException) -> None:
        if self._timeout_cm is not None:
            cm = self._timeout_cm
            self._timeout_cm = None
            try:
                await cm.__aexit__(type(exc), exc, exc.__traceback__)
            finally:
                await self._cleanup()
        else:
            await self._cleanup()

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

    async def _teardown_context(self, ctx: Any) -> None:
        unroute_all = getattr(ctx, "unroute_all", None)
        if callable(unroute_all):
            try:
                await _maybe_await(unroute_all(behavior="ignoreErrors"))
            except (PlaywrightError, RuntimeError, OSError, TypeError) as exc:
                logger.debug(f"Browser context unroute_all ignored during cleanup: {exc}")

        pending = getattr(ctx, "_ssrf_redirect_tasks", None)
        if isinstance(pending, set) and pending:
            tasks = list(pending)
            pending.clear()
            for task in tasks:
                if hasattr(task, "done") and not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        await _maybe_await(ctx.close())

    async def _cleanup(self) -> None:
        cancelled: asyncio.CancelledError | None = None
        try:
            if self.context is not None:
                ctx = self.context
                self.context = None
                cancelled = await self._run_shielded_cleanup_step(
                    self._teardown_context(ctx), "Error closing browser context"
                )

            gen_pool = getattr(self._generator, "pool", None)
            active_pool = (
                gen_pool
                if gen_pool is not None and hasattr(gen_pool, "release_browser")
                else self._pool
            )
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

"""Declarative, allowlisted browser interaction execution for Playwright pages.

Arbitrary JS evaluation is intentionally excluded to prevent XSS/SSRF sandbox escapes.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from ..models.browser_actions import (
    MAX_BROWSER_ACTIONS as MAX_BROWSER_ACTIONS,
)
from ..models.browser_actions import (
    BrowserAction as BrowserAction,
)
from ..models.browser_actions import (
    BrowserActionError as BrowserActionError,
)
from ..models.browser_actions import (
    BrowserActionType as BrowserActionType,
)
from ..models.browser_actions import (
    parse_actions_param as parse_actions_param,
)
from ..telemetry import get_tracer

logger = logging.getLogger(__name__)


async def _run_selector_action(page: Any, action: BrowserAction, timeout: int) -> None:
    assert action.selector is not None
    if action.type == "click":
        await page.click(action.selector, timeout=timeout)
    elif action.type == "fill":
        assert action.value is not None
        await page.fill(action.selector, action.value, timeout=timeout)
    elif action.type == "select":
        assert action.value is not None
        await page.select_option(action.selector, action.value, timeout=timeout)
    elif action.type == "hover":
        await page.hover(action.selector, timeout=timeout)
    else:
        await page.wait_for_selector(action.selector, timeout=timeout, state="visible")


async def _dispatch_single_action(page: Any, action: BrowserAction, timeout: int) -> None:
    if action.type == "press":
        assert action.key is not None
        if action.selector:
            await page.press(action.selector, action.key, timeout=timeout)
        else:
            await page.keyboard.press(action.key)
        return
    if action.type == "scroll":
        if action.selector:
            locator = page.locator(action.selector).first
            await locator.scroll_into_view_if_needed(timeout=timeout)
        else:
            await page.mouse.wheel(action.x, action.y)
        return
    if action.type == "wait":
        await page.wait_for_timeout(action.duration_ms)
        return
    await _run_selector_action(page, action, timeout)


async def execute_browser_actions(
    page: Any,
    actions: Sequence[BrowserAction],
    *,
    default_timeout_ms: int = 10_000,
) -> int:
    """Execute validated BrowserAction steps sequentially on a Playwright Page."""
    if not actions:
        return 0
    if len(actions) > MAX_BROWSER_ACTIONS:
        raise BrowserActionError(f"Too many browser actions: maximum is {MAX_BROWSER_ACTIONS}")

    tracer = get_tracer(__name__)
    executed = 0
    with tracer.start_as_current_span("playwright.actions") as span:
        span.set_attribute("browser.actions.count", len(actions))
        for idx, action in enumerate(actions):
            timeout = min(action.timeout_ms, default_timeout_ms)
            try:
                await _dispatch_single_action(page, action, timeout)
                executed += 1
            except (PlaywrightTimeoutError, TimeoutError) as exc:
                raise BrowserActionError(
                    f"Action #{idx} ({action.type}) timed out after {timeout}ms: {exc}"
                ) from exc
            except (PlaywrightError, RuntimeError, OSError, ValueError, AssertionError) as exc:
                raise BrowserActionError(f"Action #{idx} ({action.type}) failed: {exc}") from exc

        span.set_attribute("browser.actions.executed", executed)
    return executed

"""Browser management module for Playwright browser pooling and interaction scripting."""

from __future__ import annotations

from typing import Any

from ..models.browser_actions import (
    MAX_BROWSER_ACTIONS,
    BrowserAction,
    BrowserActionError,
    parse_actions_param,
)

__all__ = [
    "BrowserAcquireTimeoutError",
    "BrowserAction",
    "BrowserActionError",
    "BrowserConfig",
    "BrowserPool",
    "BrowserPoolError",
    "MAX_BROWSER_ACTIONS",
    "execute_browser_actions",
    "parse_actions_param",
]


def __getattr__(name: str) -> Any:
    if name in {"BrowserAcquireTimeoutError", "BrowserConfig", "BrowserPool", "BrowserPoolError"}:
        from . import manager

        return getattr(manager, name)
    if name == "execute_browser_actions":
        from . import actions

        return actions.execute_browser_actions
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

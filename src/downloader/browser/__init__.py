"""Browser management module for Playwright browser pooling and interaction scripting."""

from .actions import (
    MAX_BROWSER_ACTIONS,
    BrowserAction,
    BrowserActionError,
    execute_browser_actions,
    parse_actions_param,
)
from .manager import BrowserConfig, BrowserPool, BrowserPoolError

__all__ = [
    "BrowserAction",
    "BrowserActionError",
    "BrowserConfig",
    "BrowserPool",
    "BrowserPoolError",
    "MAX_BROWSER_ACTIONS",
    "execute_browser_actions",
    "parse_actions_param",
]

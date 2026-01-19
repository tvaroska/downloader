"""Browser management module for Playwright browser pooling."""

from .manager import BrowserConfig, BrowserPool, BrowserPoolError

__all__ = ["BrowserPool", "BrowserConfig", "BrowserPoolError"]

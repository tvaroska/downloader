"""Pure HTML JS-need detection, bounded HTML executor, and single-parse format conversion."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .config import Settings, get_settings
from .content_encoding import is_text_convertible_content_type, resolve_charset_and_decode
from .ports import (
    ContentConversionError,
    ContentTooLargeError,
    UnsupportedMediaTypeError,
)
from .ports import (
    SelectorTimeoutError as SelectorTimeoutError,
)
from .transformers.common import extract_main_content_node
from .transformers.markdown import html_to_markdown
from .transformers.plaintext import html_to_plaintext

logger = logging.getLogger(__name__)

MAX_HTML_PARSE_BYTES = 10 * 1024 * 1024

_html_executor: ThreadPoolExecutor | None = None
_html_executor_lock = threading.Lock()


def _get_html_executor() -> ThreadPoolExecutor:
    """Return a dedicated bounded thread pool so CPU-bound HTML work never starves DNS lookups."""
    global _html_executor
    with _html_executor_lock:
        if _html_executor is None:
            workers = max(2, min(4, os.cpu_count() or 2))
            _html_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="downloader-html",
            )
        return _html_executor


async def run_in_html_executor[T](func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a CPU-bound HTML parsing or conversion function on the bounded HTML executor."""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs) if kwargs else functools.partial(func, *args)
    return await loop.run_in_executor(_get_html_executor(), call)


class BoundedCache:
    """A thread-safe bounded set-like cache with LRU eviction and optional monotonic TTL."""

    def __init__(
        self,
        maxsize: int | Callable[[], int] = 1000,
        ttl: float | Callable[[], float] | None = None,
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()

    def _resolve_limits(self) -> tuple[int, float | None]:
        max_sz = self._maxsize() if callable(self._maxsize) else self._maxsize
        ttl_s = self._ttl() if callable(self._ttl) else self._ttl
        return max_sz, ttl_s

    def _purge_expired(self, now: float, ttl_s: float | None) -> None:
        if ttl_s is None or not self._cache:
            return
        expired = [k for k, ts in self._cache.items() if now - ts >= ttl_s]
        for k in expired:
            self._cache.pop(k, None)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            max_sz, ttl_s = self._resolve_limits()
            if max_sz <= 0:
                return False
            ts = self._cache.get(key)
            if ts is None:
                return False
            if ttl_s is not None and (time.monotonic() - ts) >= ttl_s:
                self._cache.pop(key, None)
                return False
            self._cache.move_to_end(key)
            return True

    def add(self, key: str) -> None:
        with self._lock:
            max_sz, ttl_s = self._resolve_limits()
            if max_sz <= 0:
                self._cache.clear()
                return
            now = time.monotonic()
            self._purge_expired(now, ttl_s)
            self._cache[key] = now
            self._cache.move_to_end(key)
            while len(self._cache) > max_sz:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            max_sz, ttl_s = self._resolve_limits()
            if max_sz <= 0:
                return 0
            self._purge_expired(time.monotonic(), ttl_s)
            return len(self._cache)


def _cache_maxsize() -> int:
    return get_settings().content.cache_max_size


def _cache_ttl() -> float:
    return float(get_settings().content.html_js_rendering_cache_ttl)


_js_heavy_cache = BoundedCache(maxsize=_cache_maxsize, ttl=_cache_ttl)
_static_html_cache = BoundedCache(maxsize=_cache_maxsize, ttl=_cache_ttl)


def _is_js_heavy_domain(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    clean_domains = [d.strip().lower().lstrip(".").rstrip(".") for d in domains if d.strip()]
    return any(host == d or host.endswith(f".{d}") for d in clean_domains)


_JS_REQUIRED_RE = re.compile(
    r"please\s+enable\s+javascript"
    r"|javascript\s+is\s+required"
    r"|\benable\s+js\b"
    r"|turn\s+on\s+javascript"
    r"|javascript\s+is\s+disabled"
    r"|requires\s+javascript",
    re.IGNORECASE,
)


def _has_missing_metadata(soup: BeautifulSoup) -> bool:
    """Return True if neither Open Graph nor Twitter Card title+description tags are present."""
    og_title = soup.find("meta", property="og:title")
    og_description = soup.find("meta", property="og:description")
    twitter_title = soup.find("meta", attrs={"name": "twitter:title"})
    twitter_description = soup.find("meta", attrs={"name": "twitter:description"})
    has_title = og_title is not None or twitter_title is not None
    has_description = og_description is not None or twitter_description is not None
    return not (has_title and has_description)


def _has_js_framework_markers(
    soup: BeautifulSoup,
    body_text: str,
    *,
    settings: Settings | None = None,
) -> bool:
    """Check for React/Vue/Angular root containers accompanied by minimal static body text."""
    react_root = soup.find(id="root")
    vue_app = soup.find(id="app")
    angular_app = soup.find(attrs={"ng-app": True})
    has_framework_marker = react_root is not None or vue_app is not None or angular_app is not None
    cfg = settings if settings is not None else get_settings()
    has_minimal_content = len(body_text) < cfg.content.min_js_framework_content_threshold
    return has_framework_marker and has_minimal_content


def _classify_parsed_html_for_js(
    url: str,
    soup: BeautifulSoup,
    content_size: int,
    settings: Settings,
) -> bool:
    noscript_text = " ".join(ns.get_text(" ", strip=True) for ns in soup.find_all("noscript"))
    missing_metadata = _has_missing_metadata(soup)

    for ignored in soup(["noscript", "script", "style", "template"]):
        ignored.decompose()

    body = soup.find("body")
    if not body:
        return False

    body_text = body.get_text(" ", strip=True)
    # Server-rendered pages that already carry substantial visible body prose do not
    # need a browser render even if <noscript> contains a generic JS notice or OG tags are absent.
    if len(body_text) > 500:
        return False

    visible_text = soup.get_text(" ", strip=True)
    return (
        bool(_JS_REQUIRED_RE.search(visible_text) or _JS_REQUIRED_RE.search(noscript_text))
        or _has_js_framework_markers(soup, body_text, settings=settings)
        or (content_size < 50000 and missing_metadata)
        or _is_js_heavy_domain(url, settings.content.html_js_heavy_domains)
    )


def needs_js_render(
    url: str,
    content: bytes,
    content_type: str,
    *,
    settings: Settings | None = None,
    soup: BeautifulSoup | None = None,
) -> bool:
    """Pure decision function: return True if static HTML likely requires JS rendering."""
    cfg = settings if settings is not None else get_settings()
    if not cfg.content.enable_html_js_rendering or "html" not in content_type.lower():
        return False

    if soup is None:
        if len(content) > MAX_HTML_PARSE_BYTES:
            return _is_js_heavy_domain(url, cfg.content.html_js_heavy_domains)
        try:
            decoded_html, _ = resolve_charset_and_decode(content, content_type)
            soup = BeautifulSoup(decoded_html, "lxml")
        except (RecursionError, ValueError, TypeError, RuntimeError) as exc:
            logger.warning(f"HTML detection failed for {url}: {exc}")
            return False

    return _classify_parsed_html_for_js(url, soup, len(content), cfg)


def should_use_playwright_for_html(url: str, content: bytes, content_type: str) -> bool:
    """Detect whether static HTML bytes likely require headless Chromium rendering."""
    settings = get_settings()
    if not settings.content.enable_html_js_rendering:
        return False
    if url in _js_heavy_cache:
        return True
    if url in _static_html_cache or "html" not in content_type.lower():
        return False

    needs_js = needs_js_render(url, content, content_type, settings=settings)
    if needs_js:
        _js_heavy_cache.add(url)
    else:
        _static_html_cache.add(url)
    return needs_js


def _convert_html_to_format(html_content: str, output_format: Literal["text", "markdown"]) -> str:
    """Parse HTML once and convert the extracted main content node to `text` or `markdown`."""
    main_content = extract_main_content_node(html_content)
    if output_format == "markdown":
        markdown = html_to_markdown(main_content)
        if not markdown:
            text = main_content.get_text(separator="\n", strip=True)
            return re.sub(r"\n\s*\n+", "\n\n", text).strip()
        return markdown
    return html_to_plaintext(main_content)


def convert_content(
    content: bytes,
    content_type: str,
    output_format: Literal["text", "markdown"] = "text",
) -> str:
    """Convert raw upstream bytes to `text` or `markdown`, raising `ContentConversionError` on DOM failure."""
    if not is_text_convertible_content_type(
        content_type,
        allow_ocr_images=False,
        sample_bytes=content,
    ):
        raise UnsupportedMediaTypeError(content_type, target_format=output_format)

    text, _ = resolve_charset_and_decode(content, content_type)

    ct_lower = content_type.lower()
    if "html" in ct_lower or "xhtml" in ct_lower:
        if len(content) > MAX_HTML_PARSE_BYTES:
            raise ContentTooLargeError(
                f"HTML content size ({len(content)} bytes) exceeds parse limit "
                f"({MAX_HTML_PARSE_BYTES} bytes)"
            )
        try:
            return _convert_html_to_format(text, output_format)
        except ContentConversionError:
            raise
        except (RecursionError, ValueError, TypeError, RuntimeError, AttributeError) as exc:
            logger.error(f"HTML {output_format} conversion failed: {exc}")
            raise ContentConversionError(
                f"Failed to convert HTML content to {output_format}: {exc}"
            ) from exc

    return text

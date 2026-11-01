"""Origin robots.txt fetcher, bounded TTL/LRU + optional Redis cache, and RFC 9309 evaluator."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from ..metrics import (
    record_robots_cache_hit,
    record_robots_disallowed,
    record_robots_evaluation,
)
from ..validation import SSRFProtectionError, sanitize_user_agent, validate_url

logger = logging.getLogger(__name__)

_VALID_DIRECTIVES = frozenset(
    {"user-agent", "disallow", "allow", "crawl-delay", "request-rate", "sitemap", "host"}
)


class RobotsDisallowedError(Exception):
    """Raised when an origin's robots.txt disallows fetching a URL for the active User-Agent."""

    def __init__(
        self,
        url: str,
        user_agent: str = "REST-API-Downloader/1.0",
        reason: str | None = None,
    ) -> None:
        self.url = url
        self.user_agent = user_agent
        self.reason = reason or f"Disallowed by robots.txt for User-Agent '{user_agent}': {url}"
        super().__init__(self.reason)


def _is_malformed_robots_txt(raw_text: str) -> bool:
    """Detect binary payloads, HTML error pages, or non-directive garbage returned as 200 OK."""
    if "\x00" in raw_text:
        return True

    stripped_lead = raw_text.lstrip()[:32].lower()
    if stripped_lead.startswith(("<!doctype html", "<html", "<?xml")):
        return True

    non_comment_lines = []
    has_known_directive = False
    for raw_line in raw_text.splitlines():
        comment_idx = raw_line.find("#")
        line = raw_line[:comment_idx].strip() if comment_idx >= 0 else raw_line.strip()
        if not line:
            continue
        non_comment_lines.append(line)
        if ":" in line:
            directive = line.split(":", 1)[0].strip().lower()
            if directive in _VALID_DIRECTIVES:
                has_known_directive = True

    if non_comment_lines and not has_known_directive:
        return True
    return False


def _extract_crawl_delay(
    rp: RobotFileParser,
    raw_text: str,
    user_agent: str,
) -> float | None:
    """Extract integer or fractional Crawl-delay matching RobotFileParser user-agent precedence."""
    stdlib_delay = rp.crawl_delay(user_agent)
    if stdlib_delay is not None:
        return float(stdlib_delay)

    # Python's RobotFileParser.parse checks str.isdecimal() and drops float Crawl-delay values.
    groups: list[tuple[list[str], float | None]] = []
    state = 0
    current_agents: list[str] = []
    current_delay: float | None = None

    for raw_line in raw_text.splitlines():
        comment_idx = raw_line.find("#")
        line = raw_line[:comment_idx].strip() if comment_idx >= 0 else raw_line.strip()
        if not line or ":" not in line:
            continue
        key_raw, val_raw = line.split(":", 1)
        key = key_raw.strip().lower()
        val = val_raw.strip()

        if key == "user-agent":
            if state == 2:
                groups.append((current_agents, current_delay))
                current_agents = []
                current_delay = None
            current_agents.append(val)
            state = 1
        elif key in ("disallow", "allow", "request-rate"):
            if state != 0:
                state = 2
        elif key == "crawl-delay":
            if state != 0:
                state = 2
                try:
                    parsed_delay = float(val)
                    if math.isfinite(parsed_delay) and parsed_delay >= 0.0:
                        current_delay = parsed_delay
                except ValueError:
                    pass

    if state != 0:
        groups.append((current_agents, current_delay))

    ua_lower = user_agent.lower()
    ua_product = user_agent.split("/", 1)[0].lower()

    for agents, delay in groups:
        if any(a.lower() == ua_lower for a in agents):
            return delay

    for agents, delay in groups:
        for agent in agents:
            if agent != "*" and agent.lower() in ua_product:
                return delay

    for agents, delay in groups:
        if "*" in agents:
            return delay

    return None


class RobotsChecker:
    """Stateless-first robots.txt evaluator with bounded LRU+TTL memory cache and optional Redis."""

    def __init__(
        self,
        user_agent: str = "REST-API-Downloader/1.0",
        *,
        robots_cache_ttl: int | None = None,
        robots_cache_max_size: int | None = None,
        robots_fetch_timeout: float | None = None,
        max_crawl_delay: float = 30.0,
        redis_client: Any | None = None,
        fetcher: Callable[[str], Awaitable[tuple[int, str]]] | None = None,
        cache_ttl: int | None = None,
        cache_max_size: int | None = None,
        fetch_timeout: float | None = None,
        ttl: int | None = None,
        maxsize: int | None = None,
        timeout: float | None = None,
    ) -> None:
        self.user_agent = sanitize_user_agent(user_agent)
        resolved_ttl = next(
            (v for v in (cache_ttl, robots_cache_ttl, ttl) if v is not None),
            3600,
        )
        resolved_max_size = next(
            (v for v in (cache_max_size, robots_cache_max_size, maxsize) if v is not None),
            1000,
        )
        resolved_timeout = next(
            (v for v in (fetch_timeout, robots_fetch_timeout, timeout) if v is not None),
            5.0,
        )
        self.cache_ttl: int = int(resolved_ttl)
        self.cache_max_size: int = max(1, int(resolved_max_size))
        self.fetch_timeout: float = float(resolved_timeout)
        self.max_crawl_delay: float = float(max_crawl_delay)
        self.redis_client = redis_client
        self._fetcher = fetcher

        # (expires_at_monotonic, raw_robots_txt, allow_all)
        self._cache: OrderedDict[str, tuple[float, str, bool]] = OrderedDict()
        self._origin_locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._http_client: httpx.AsyncClient | None = None

    def _get_origin_lock(self, cache_key: str) -> asyncio.Lock:
        lock = self._origin_locks.get(cache_key)
        if lock is not None:
            self._origin_locks.move_to_end(cache_key)
            return lock

        lock = asyncio.Lock()
        self._origin_locks[cache_key] = lock
        while len(self._origin_locks) > self.cache_max_size:
            oldest_key, oldest_lock = next(iter(self._origin_locks.items()))
            if oldest_key == cache_key or oldest_lock.locked():
                break
            self._origin_locks.popitem(last=False)
        return lock

    def _get_memory_cache(self, cache_key: str) -> tuple[str, bool] | None:
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        expires_at, raw_txt, allow_all = entry
        if time.monotonic() >= expires_at:
            self._cache.pop(cache_key, None)
            return None
        self._cache.move_to_end(cache_key)
        return raw_txt, allow_all

    def _set_memory_cache(self, cache_key: str, raw_txt: str, allow_all: bool) -> None:
        expires_at = time.monotonic() + self.cache_ttl
        self._cache[cache_key] = (expires_at, raw_txt, allow_all)
        self._cache.move_to_end(cache_key)
        while len(self._cache) > self.cache_max_size:
            self._cache.popitem(last=False)

    async def _get_cached_entry(self, netloc: str) -> tuple[str, bool] | None:
        if self.redis_client is not None:
            redis_key = f"robots:v1:{netloc}"
            try:
                raw_cached = await self.redis_client.get(redis_key)
                if isinstance(raw_cached, bytes):
                    raw_cached = raw_cached.decode("utf-8", errors="ignore")
                if isinstance(raw_cached, str):
                    try:
                        parsed = json.loads(raw_cached)
                        if isinstance(parsed, dict) and "allow_all" in parsed:
                            return str(parsed.get("raw", "")), bool(parsed["allow_all"])
                    except ValueError:
                        pass
                    return raw_cached, False
                return None
            except Exception as exc:
                logger.warning(
                    f"Redis robots.txt cache read failed for {netloc}, using memory cache: {exc}"
                )

        return self._get_memory_cache(netloc)

    async def _set_cached_entry(self, netloc: str, raw_txt: str, allow_all: bool) -> None:
        self._set_memory_cache(netloc, raw_txt, allow_all)
        if self.redis_client is not None:
            redis_key = f"robots:v1:{netloc}"
            payload = json.dumps({"raw": raw_txt, "allow_all": allow_all})
            try:
                await self.redis_client.set(redis_key, payload, ex=self.cache_ttl)
            except Exception as exc:
                logger.warning(
                    f"Redis robots.txt cache write failed for {netloc}, kept in memory cache: {exc}"
                )

    async def _on_redirect_hook(self, response: httpx.Response) -> None:
        if response.is_redirect and "location" in response.headers:
            next_url = str(httpx.URL(str(response.url)).join(response.headers["location"]))
            validate_url(next_url)

    def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self.fetch_timeout,
                follow_redirects=True,
                max_redirects=5,
                event_hooks={"response": [self._on_redirect_hook]},
                headers={"User-Agent": self.user_agent, "Accept": "text/plain,*/*"},
            )
        return self._http_client

    async def _default_fetch(self, robots_url: str) -> tuple[int, str]:
        client = self._ensure_http_client()
        async with client.stream("GET", robots_url) as response:
            chunks: list[bytes] = []
            total = 0
            # RFC 9309 §2.5 requires crawlers to parse at least 500 KiB of robots.txt
            max_robots_bytes = 512 * 1024
            async for chunk in response.aiter_bytes():
                remaining = max_robots_bytes - total
                if remaining <= 0:
                    break
                chunks.append(chunk[:remaining])
                total += len(chunks[-1])
            body = b"".join(chunks).decode("utf-8", errors="replace")
            return response.status_code, body

    async def _fetch_and_cache(self, scheme: str, netloc: str) -> tuple[str, bool]:
        robots_url = f"{scheme}://{netloc}/robots.txt"

        try:
            validate_url(robots_url)
        except SSRFProtectionError as exc:
            # Fail open when real DNS lookup fails; if an explicit test fetcher is injected,
            # still block localhost/restricted hostnames while allowing synthetic domain names.
            host_only = netloc.split(":", 1)[0].strip("[]").lower()
            if "DNS resolution failed" in str(exc):
                if self._fetcher is not None and not host_only.endswith("localhost"):
                    pass
                else:
                    logger.warning(
                        f"DNS resolution failed fetching {robots_url} ({exc}); failing open"
                    )
                    await self._set_cached_entry(netloc, "", True)
                    return "", True
            else:
                raise

        try:
            if self._fetcher is not None:
                status_code, raw_text = await self._fetcher(robots_url)
            else:
                status_code, raw_text = await self._default_fetch(robots_url)
        except SSRFProtectionError:
            raise
        except Exception as exc:
            if self._fetcher is None and "network is unreachable" in str(exc).lower():
                logger.debug(f"Offline environment fetching {robots_url} ({exc}); failing open")
            else:
                logger.warning(f"Unreachable robots.txt at {robots_url} ({exc}); failing open")
            await self._set_cached_entry(netloc, "", True)
            return "", True

        if status_code >= 400:
            logger.warning(f"robots.txt at {robots_url} returned HTTP {status_code}; failing open")
            await self._set_cached_entry(netloc, "", True)
            return "", True

        if _is_malformed_robots_txt(raw_text):
            logger.warning(f"Malformed robots.txt at {robots_url}; failing open")
            await self._set_cached_entry(netloc, "", True)
            return "", True

        await self._set_cached_entry(netloc, raw_text, False)
        return raw_text, False

    async def check_allowed(self, url: str, user_agent: str | None = None) -> float | None:
        """
        Evaluate whether `url` is allowed by origin's `/robots.txt` for `user_agent`.

        Returns:
            Optional Crawl-delay in seconds (capped at `max_crawl_delay`) when allowed.

        Raises:
            RobotsDisallowedError: When disallowed by origin's `robots.txt`.
            URLValidationError: When origin or redirect violates SSRF rules.
        """
        record_robots_evaluation()
        effective_ua = sanitize_user_agent(user_agent) if user_agent else self.user_agent

        parsed = urlparse(url)
        scheme = (parsed.scheme or "https").lower()
        netloc = parsed.netloc.lower()
        if not netloc:
            return None

        cached = await self._get_cached_entry(netloc)
        if cached is not None:
            record_robots_cache_hit()
            raw_txt, allow_all = cached
        else:
            lock = self._get_origin_lock(netloc)
            async with lock:
                cached = await self._get_cached_entry(netloc)
                if cached is not None:
                    record_robots_cache_hit()
                    raw_txt, allow_all = cached
                else:
                    raw_txt, allow_all = await self._fetch_and_cache(scheme, netloc)

        if allow_all or not raw_txt.strip():
            return None

        try:
            rp = RobotFileParser()
            rp.parse(raw_txt.splitlines())
            allowed = rp.can_fetch(effective_ua, url)
        except Exception as exc:
            logger.warning(f"Error parsing robots.txt for {netloc} ({exc}); failing open")
            return None

        if not allowed:
            record_robots_disallowed()
            raise RobotsDisallowedError(url=url, user_agent=effective_ua)

        delay = _extract_crawl_delay(rp, raw_txt, effective_ua)
        if delay is None:
            return None
        return min(delay, self.max_crawl_delay)

    async def close(self) -> None:
        """Close dedicated HTTP client if initialized."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

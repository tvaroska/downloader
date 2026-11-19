"""Origin robots.txt fetcher, byte-bounded TTL/LRU + Redis cache, and RFC 9309 evaluator."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit

import httpcore
import httpx
from redis.exceptions import RedisError

from ..content_encoding import (
    make_stateless_cookies,
    read_bounded_response_bytes,
    resolve_charset_and_decode,
)
from ..metrics import (
    record_robots_cache_hit,
    record_robots_disallowed,
    record_robots_evaluation,
)
from ..ports import RobotsDisallowedError as RobotsDisallowedError
from ..ssrf_transport import SSRFSafeHTTPTransport, SSRFSafeNetworkBackend
from ..validation import (
    DNSResolutionError,
    EgressPolicy,
    URLValidationError,
    canonical_origin,
    format_origin_url,
    redact_url_for_log,
    sanitize_user_agent,
    validate_url_async,
)

logger = logging.getLogger(__name__)

_VALID_DIRECTIVES = frozenset(
    {"user-agent", "disallow", "allow", "crawl-delay", "request-rate", "sitemap", "host"}
)
_DEFAULT_NEGATIVE_CACHE_TTL = 60
_DEFAULT_MAX_CACHE_BYTES = 8 * 1024 * 1024
_MAX_ROBOTS_TXT_BYTES = 512 * 1024

OriginTuple = tuple[str, str, int]


@dataclass
class _RobotsGroup:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)
    crawl_delay: float | None = None


def _strip_comment(raw_line: str) -> str:
    idx = raw_line.find("#")
    return (raw_line[:idx] if idx >= 0 else raw_line).strip()


def _is_malformed_robots_txt(raw_text: str) -> bool:
    """Detect binary payloads, HTML error pages, or non-directive garbage returned as 200 OK."""
    raw_text = raw_text.removeprefix("\ufeff")
    if "\x00" in raw_text:
        return True
    stripped_lead = raw_text.lstrip()[:32].lower()
    if stripped_lead.startswith(("<!doctype html", "<html", "<?xml")):
        return True

    has_non_comment = False
    has_known_directive = False
    for raw_line in raw_text.splitlines():
        line = _strip_comment(raw_line)
        if not line:
            continue
        has_non_comment = True
        if ":" in line and line.split(":", 1)[0].strip().lower() in _VALID_DIRECTIVES:
            has_known_directive = True
    return has_non_comment and not has_known_directive


def _parse_crawl_delay_value(val: str) -> float | None:
    try:
        parsed = float(val)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _parse_robots_groups(raw_text: str) -> list[_RobotsGroup]:
    """Parse robots.txt lines into RFC 9309 user-agent rule groups."""
    raw_text = raw_text.removeprefix("\ufeff")
    groups: list[_RobotsGroup] = []
    current: _RobotsGroup | None = None
    in_rules = False

    for raw_line in raw_text.splitlines():
        line = _strip_comment(raw_line)
        if not line or ":" not in line:
            continue
        key_raw, val_raw = line.split(":", 1)
        key = key_raw.strip().lower()
        val = val_raw.strip()

        if key == "user-agent":
            if current is None or in_rules:
                current = _RobotsGroup()
                groups.append(current)
                in_rules = False
            current.agents.append(val)
        elif current is not None and key in {"allow", "disallow"}:
            in_rules = True
            current.rules.append((key == "allow", val))
        elif current is not None and key == "crawl-delay":
            in_rules = True
            delay = _parse_crawl_delay_value(val)
            if delay is not None:
                current.crawl_delay = delay
        elif current is not None and key == "request-rate":
            in_rules = True

    return groups


def _select_matching_groups(groups: list[_RobotsGroup], user_agent: str) -> list[_RobotsGroup]:
    """Select the most specific RFC 9309 §2.2.1 group(s) matching `user_agent`."""
    ua_product = user_agent.split("/", 1)[0].strip().lower()
    ua_full = user_agent.strip().lower()

    best_len = 0
    specific: list[_RobotsGroup] = []
    wildcard: list[_RobotsGroup] = []

    for grp in groups:
        matched_len = 0
        for raw_agent in grp.agents:
            token = raw_agent.strip().lower()
            if not token:
                continue
            if token == "*":
                wildcard.append(grp)
                continue
            token_product = token.split("/", 1)[0].strip()
            if token == ua_full or (token_product and token_product in ua_product):
                matched_len = max(matched_len, len(token_product))
        if matched_len > best_len:
            best_len = matched_len
            specific = [grp]
        elif matched_len > 0 and matched_len == best_len:
            specific.append(grp)

    return specific if specific else wildcard


def _normalize_robots_path(raw: str) -> str:
    """Normalize percent-encoding case for deterministic RFC 9309 path comparison."""
    return unquote(raw)


def _pattern_matches(pattern: str, target_path: str) -> bool:
    """Match an RFC 9309 §2.2.2 path pattern supporting `*` wildcards and trailing `$`."""
    anchored = pattern.endswith("$")
    core = pattern[:-1] if anchored else pattern
    norm_core = _normalize_robots_path(core)
    norm_target = _normalize_robots_path(target_path)
    regex_body = ".*".join(re.escape(part) for part in norm_core.split("*"))
    expr = f"^{regex_body}{'$' if anchored else ''}"
    return re.match(expr, norm_target) is not None


def _evaluate_robots_groups(
    groups: list[_RobotsGroup],
    user_agent: str,
    url: str,
) -> tuple[bool, float | None]:
    """Evaluate `url` against pre-parsed `_RobotsGroup`s using RFC 9309 rules."""
    selected = _select_matching_groups(groups, user_agent)
    if not selected:
        return True, None

    parsed = urlsplit(url)
    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"

    matched_rules: list[tuple[int, bool]] = []
    crawl_delay: float | None = None
    for grp in selected:
        if crawl_delay is None and grp.crawl_delay is not None:
            crawl_delay = grp.crawl_delay
        for is_allow, pattern in grp.rules:
            if not pattern:
                continue
            if _pattern_matches(pattern, target):
                matched_rules.append((len(pattern), is_allow))

    if not matched_rules:
        return True, crawl_delay

    # RFC 9309 §2.2.2: longest matching pattern wins; equal-length ties favor Allow.
    _, allowed = max(matched_rules, key=lambda item: (item[0], item[1]))
    return allowed, crawl_delay


def _evaluate_robots_txt(
    raw_text: str,
    user_agent: str,
    url: str,
) -> tuple[bool, float | None]:
    """Evaluate `url` against `raw_text` using RFC 9309 longest-match and `*`/`$` rules."""
    groups = _parse_robots_groups(raw_text)
    return _evaluate_robots_groups(groups, user_agent, url)


def _format_redis_robots_key(origin: OriginTuple) -> str:
    scheme, host, port = origin
    host_part = f"[{host}]" if ":" in host else host
    is_default = (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    return (
        f"robots:v2:{scheme}:{host_part}"
        if is_default
        else f"robots:v2:{scheme}:{host_part}:{port}"
    )


class RobotsChecker:
    """Stateless-first robots.txt evaluator with byte-bounded LRU+TTL memory cache and Redis."""

    def __init__(  # noqa: PLR0913
        self,
        user_agent: str = "REST-API-Downloader/1.0",
        *,
        robots_cache_ttl: int = 3600,
        negative_cache_ttl: int = _DEFAULT_NEGATIVE_CACHE_TTL,
        robots_cache_max_size: int = 1000,
        max_cache_bytes: int = _DEFAULT_MAX_CACHE_BYTES,
        robots_fetch_timeout: float = 5.0,
        max_crawl_delay: float = 30.0,
        redis_client: Any | None = None,
        fetcher: Callable[[str], Awaitable[tuple[int, str]]] | None = None,
        egress_policy: EgressPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self.user_agent = sanitize_user_agent(user_agent)
        self.cache_ttl: int = max(1, int(robots_cache_ttl))
        self.negative_cache_ttl: int = max(1, min(int(negative_cache_ttl), self.cache_ttl))
        self.cache_max_size: int = max(1, int(robots_cache_max_size))
        self.max_cache_bytes: int = max(1024, int(max_cache_bytes))
        self.fetch_timeout: float = float(robots_fetch_timeout)
        self.max_crawl_delay: float = float(max_crawl_delay)
        self.redis_client = redis_client
        self._fetcher = fetcher
        self._egress_policy = egress_policy
        self._transport = transport
        self._network_backend = network_backend

        # (expires_at_monotonic, raw_robots_txt, allow_all, byte_len, parsed_groups)
        self._cache: OrderedDict[OriginTuple, tuple[float, str, bool, int, list[_RobotsGroup]]] = (
            OrderedDict()
        )
        self._cached_bytes: int = 0
        self._origin_locks: OrderedDict[OriginTuple, asyncio.Lock] = OrderedDict()
        self._http_client: httpx.AsyncClient | None = None

    def _get_origin_lock(self, origin: OriginTuple) -> asyncio.Lock:
        lock = self._origin_locks.get(origin)
        if lock is not None:
            self._origin_locks.move_to_end(origin)
            return lock

        lock = asyncio.Lock()
        self._origin_locks[origin] = lock
        while len(self._origin_locks) > self.cache_max_size:
            oldest_key, oldest_lock = next(iter(self._origin_locks.items()))
            if oldest_key == origin or oldest_lock.locked():
                break
            self._origin_locks.popitem(last=False)
        return lock

    def _evict_entry(self, origin: OriginTuple) -> None:
        removed = self._cache.pop(origin, None)
        if removed is not None:
            self._cached_bytes = max(0, self._cached_bytes - removed[3])

    def _get_memory_cache(self, origin: OriginTuple) -> tuple[str, bool, list[_RobotsGroup]] | None:
        entry = self._cache.get(origin)
        if entry is None:
            return None
        expires_at, raw_txt, allow_all, _, parsed_groups = entry
        if time.monotonic() >= expires_at:
            self._evict_entry(origin)
            return None
        self._cache.move_to_end(origin)
        return raw_txt, allow_all, parsed_groups

    def _set_memory_cache(
        self,
        origin: OriginTuple,
        raw_txt: str,
        allow_all: bool,
        ttl: int,
        parsed_groups: list[_RobotsGroup] | None = None,
    ) -> list[_RobotsGroup]:
        encoded_len = len(raw_txt.encode("utf-8"))
        if encoded_len > self.max_cache_bytes:
            raw_txt = raw_txt.encode("utf-8")[: self.max_cache_bytes].decode("utf-8", "ignore")
            encoded_len = len(raw_txt.encode("utf-8"))
            parsed_groups = None

        if parsed_groups is None:
            parsed_groups = (
                [] if allow_all or not raw_txt.strip() else _parse_robots_groups(raw_txt)
            )

        self._evict_entry(origin)
        expires_at = time.monotonic() + ttl
        self._cache[origin] = (expires_at, raw_txt, allow_all, encoded_len, parsed_groups)
        self._cached_bytes += encoded_len
        self._cache.move_to_end(origin)

        while len(self._cache) > self.cache_max_size or (
            len(self._cache) > 1 and self._cached_bytes > self.max_cache_bytes
        ):
            oldest_key = next(iter(self._cache))
            self._evict_entry(oldest_key)
        return parsed_groups

    async def _get_cached_entry(
        self, origin: OriginTuple
    ) -> tuple[str, bool, list[_RobotsGroup]] | None:
        if self.redis_client is not None:
            redis_key = _format_redis_robots_key(origin)
            origin_label = format_origin_url(*origin)
            try:
                raw_cached = await self.redis_client.get(redis_key)
                if isinstance(raw_cached, bytes):
                    raw_cached = raw_cached.decode("utf-8", errors="ignore")
                if isinstance(raw_cached, str):
                    txt = raw_cached
                    allow_all = False
                    try:
                        parsed = json.loads(raw_cached)
                        if isinstance(parsed, dict) and "allow_all" in parsed:
                            txt = str(parsed.get("raw", ""))
                            allow_all = bool(parsed["allow_all"])
                    except ValueError:
                        pass
                    mem = self._get_memory_cache(origin)
                    if mem is not None and mem[0] == txt and mem[1] == allow_all:
                        return mem
                    groups = self._set_memory_cache(origin, txt, allow_all, self.negative_cache_ttl)
                    return txt, allow_all, groups
                return None
            except (RedisError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    f"Redis robots.txt cache read failed for {origin_label}, using memory cache: {exc}"
                )

        return self._get_memory_cache(origin)

    async def _set_cached_entry(
        self,
        origin: OriginTuple,
        raw_txt: str,
        allow_all: bool,
        *,
        ttl: int,
    ) -> list[_RobotsGroup]:
        parsed_groups = self._set_memory_cache(origin, raw_txt, allow_all, ttl)
        if self.redis_client is not None:
            redis_key = _format_redis_robots_key(origin)
            origin_label = format_origin_url(*origin)
            payload = json.dumps({"raw": raw_txt, "allow_all": allow_all})
            try:
                await self.redis_client.set(redis_key, payload, ex=ttl)
            except (RedisError, OSError, RuntimeError, ValueError) as exc:
                logger.warning(
                    f"Redis robots.txt cache write failed for {origin_label}, kept in memory cache: {exc}"
                )
        return parsed_groups

    async def _validate_candidate_url(self, target_url: str) -> str:
        if self._egress_policy is not None:
            return await self._egress_policy.validate_url(target_url)
        return await validate_url_async(target_url)

    async def _on_redirect_hook(self, response: httpx.Response) -> None:
        if response.is_redirect and "location" in response.headers:
            next_url = str(httpx.URL(str(response.url)).join(response.headers["location"]))
            await self._validate_candidate_url(next_url)

    def _ensure_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            backend: httpcore.AsyncNetworkBackend | None = None
            if self._network_backend is not None:
                backend = (
                    self._network_backend
                    if isinstance(self._network_backend, SSRFSafeNetworkBackend)
                    else SSRFSafeNetworkBackend(
                        inner=self._network_backend,
                        egress_policy=self._egress_policy,
                    )
                )
            active_transport = self._transport or SSRFSafeHTTPTransport(
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                http2=False,
                backend=backend,
                egress_policy=self._egress_policy,
            )
            self._http_client = httpx.AsyncClient(
                transport=active_transport,
                timeout=self.fetch_timeout,
                follow_redirects=True,
                max_redirects=5,
                trust_env=False,
                cookies=make_stateless_cookies(),
                event_hooks={"response": [self._on_redirect_hook]},
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/plain,*/*",
                    "Accept-Encoding": "gzip, deflate, br",
                },
            )
        elif isinstance(self._http_client, httpx.AsyncClient):
            self._http_client._cookies = make_stateless_cookies()
        return self._http_client

    async def _default_fetch(self, robots_url: str) -> tuple[int, str]:
        client = self._ensure_http_client()
        async with client.stream("GET", robots_url) as response:
            raw_bytes = await read_bounded_response_bytes(
                response,
                max_bytes=_MAX_ROBOTS_TXT_BYTES,
                on_too_large=lambda actual: ValueError(
                    f"robots.txt exceeded {_MAX_ROBOTS_TXT_BYTES} bytes ({actual})"
                ),
                on_decode_error=ValueError,
                truncate_on_limit=True,
            )
            body, _ = resolve_charset_and_decode(raw_bytes, response.headers.get("content-type"))
            return response.status_code, body

    async def _cache_fail_open(
        self,
        origin: OriginTuple,
        warning_msg: str,
        *,
        ttl: int | None = None,
    ) -> tuple[str, bool, list[_RobotsGroup]]:
        if warning_msg:
            logger.warning(warning_msg)
        effective_ttl = self.negative_cache_ttl if ttl is None else ttl
        groups = await self._set_cached_entry(origin, "", True, ttl=effective_ttl)
        return "", True, groups

    async def _fetch_and_cache(self, origin: OriginTuple) -> tuple[str, bool, list[_RobotsGroup]]:
        robots_url = f"{format_origin_url(*origin)}/robots.txt"
        safe_robots_url = redact_url_for_log(robots_url)

        try:
            await self._validate_candidate_url(robots_url)
            status_code, raw_text = (
                await self._fetcher(robots_url)
                if self._fetcher is not None
                else await self._default_fetch(robots_url)
            )
            raw_text = raw_text.removeprefix("\ufeff")
        except DNSResolutionError as exc:
            return await self._cache_fail_open(
                origin,
                f"DNS resolution failed fetching {safe_robots_url} ({exc}); failing open",
            )
        except URLValidationError:
            raise
        except (httpx.HTTPError, OSError, TimeoutError, ValueError, RuntimeError) as exc:
            return await self._cache_fail_open(
                origin,
                f"Unreachable robots.txt at {safe_robots_url} ({exc}); failing open",
            )

        if status_code in {404, 410}:
            return await self._cache_fail_open(origin, "", ttl=self.cache_ttl)
        if status_code >= 400:
            return await self._cache_fail_open(
                origin,
                f"robots.txt at {safe_robots_url} returned HTTP {status_code}; failing open",
            )
        if _is_malformed_robots_txt(raw_text):
            return await self._cache_fail_open(
                origin,
                f"Malformed robots.txt at {safe_robots_url}; failing open",
            )

        groups = await self._set_cached_entry(origin, raw_text, False, ttl=self.cache_ttl)
        return raw_text, False, groups

    async def check_allowed(self, url: str, user_agent: str | None = None) -> float | None:
        """Evaluate whether `url` is allowed by origin's `/robots.txt` for `user_agent`."""
        record_robots_evaluation()
        effective_ua = sanitize_user_agent(user_agent) if user_agent else self.user_agent

        origin = canonical_origin(url)
        if origin is None:
            return None

        cached = await self._get_cached_entry(origin)
        if cached is not None:
            record_robots_cache_hit()
            raw_txt, allow_all, parsed_groups = cached
        else:
            lock = self._get_origin_lock(origin)
            async with lock:
                cached = await self._get_cached_entry(origin)
                if cached is not None:
                    record_robots_cache_hit()
                    raw_txt, allow_all, parsed_groups = cached
                else:
                    raw_txt, allow_all, parsed_groups = await self._fetch_and_cache(origin)

        if allow_all or not raw_txt.strip():
            return None

        allowed, delay = _evaluate_robots_groups(parsed_groups, effective_ua, url)
        if not allowed:
            record_robots_disallowed()
            raise RobotsDisallowedError(url=url, user_agent=effective_ua)

        return min(delay, self.max_crawl_delay) if delay is not None else None

    async def __aenter__(self) -> RobotsChecker:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Close dedicated HTTP client if initialized."""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

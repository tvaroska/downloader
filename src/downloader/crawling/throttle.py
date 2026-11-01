"""Per-domain Crawl-delay throttling with bounded LRU in-memory slots and optional Redis."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class DomainThrottle:
    """Enforces per-domain Crawl-delay pacing across single and batch downloads."""

    def __init__(
        self,
        *,
        max_crawl_delay: float = 30.0,
        max_size: int = 1000,
        maxsize: int | None = None,
        max_domains: int | None = None,
        redis_client: Any | None = None,
    ) -> None:
        resolved_max = next(
            (v for v in (maxsize, max_domains) if v is not None),
            max_size,
        )
        self.max_crawl_delay = float(max_crawl_delay)
        self.max_size = max(1, int(resolved_max))
        self.redis_client = redis_client

        self._next_allowed: OrderedDict[str, float] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        if "://" in domain:
            return urlparse(domain).netloc.lower()
        return domain.strip().lower()

    def _get_lock(self, domain_key: str) -> asyncio.Lock:
        lock = self._locks.get(domain_key)
        if lock is not None:
            self._locks.move_to_end(domain_key)
            return lock

        lock = asyncio.Lock()
        self._locks[domain_key] = lock
        while len(self._locks) > self.max_size:
            oldest_key, oldest_lock = next(iter(self._locks.items()))
            if oldest_key == domain_key or oldest_lock.locked():
                break
            self._locks.popitem(last=False)
        return lock

    def _reserve_memory_slot(self, domain_key: str, delay: float) -> float:
        now = time.monotonic()
        next_slot = self._next_allowed.get(domain_key, 0.0)
        if now >= next_slot:
            wait_seconds = 0.0
            new_slot = now + delay
        else:
            wait_seconds = next_slot - now
            new_slot = next_slot + delay

        self._next_allowed[domain_key] = new_slot
        self._next_allowed.move_to_end(domain_key)
        while len(self._next_allowed) > self.max_size:
            self._next_allowed.popitem(last=False)
        return wait_seconds

    async def _reserve_redis_slot(self, domain_key: str, delay: float) -> float:
        assert self.redis_client is not None
        redis_key = f"throttle:v1:{domain_key}"
        now_wall = time.time()

        raw_val = await self.redis_client.get(redis_key)
        if isinstance(raw_val, bytes):
            raw_val = raw_val.decode("utf-8", errors="ignore")

        next_wall = 0.0
        if isinstance(raw_val, (str, int, float)):
            try:
                next_wall = float(raw_val)
            except ValueError:
                next_wall = 0.0

        if now_wall >= next_wall:
            wait_seconds = 0.0
            new_next_wall = now_wall + delay
        else:
            wait_seconds = next_wall - now_wall
            new_next_wall = next_wall + delay

        ttl_ms = max(1, int((new_next_wall - now_wall) * 1000))
        if hasattr(self.redis_client, "psetex"):
            await self.redis_client.psetex(redis_key, ttl_ms, str(new_next_wall))
        else:
            await self.redis_client.set(redis_key, str(new_next_wall), px=ttl_ms)
        return wait_seconds

    async def wait_if_needed(self, domain: str, crawl_delay: float | None) -> float:
        """Sleep until the next allowed request slot for `domain` if `crawl_delay` is positive."""
        if crawl_delay is None or crawl_delay <= 0.0:
            return 0.0

        domain_key = self._normalize_domain(domain)
        if not domain_key:
            return 0.0

        delay = min(float(crawl_delay), self.max_crawl_delay)
        if delay <= 0.0:
            return 0.0

        lock = self._get_lock(domain_key)
        async with lock:
            if self.redis_client is not None:
                try:
                    wait_seconds = await self._reserve_redis_slot(domain_key, delay)
                except Exception as exc:
                    logger.warning(
                        f"Redis domain throttle failed for {domain_key}, using memory slot: {exc}"
                    )
                    wait_seconds = self._reserve_memory_slot(domain_key, delay)
            else:
                wait_seconds = self._reserve_memory_slot(domain_key, delay)

        if wait_seconds > 0.0:
            await asyncio.sleep(wait_seconds)
        return wait_seconds

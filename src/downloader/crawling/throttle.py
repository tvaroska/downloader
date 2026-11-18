"""Per-domain Crawl-delay throttling with bounded queue wait, canonical host keys, and atomic Redis Lua."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any

from redis.exceptions import RedisError

from ..validation import canonical_host

logger = logging.getLogger(__name__)

_RESERVE_SLOT_LUA = """
local key = KEYS[1]
local now_wall = tonumber(ARGV[1])
local delay = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local raw = redis.call('GET', key)
local next_wall = 0.0
if raw then
  local parsed = tonumber(raw)
  if parsed then
    next_wall = parsed
  end
end
local wait_s = 0.0
local new_next = now_wall + delay
if now_wall < next_wall then
  wait_s = next_wall - now_wall
  if max_wait ~= nil and wait_s > max_wait then
    return tostring(-wait_s)
  end
  new_next = next_wall + delay
end
local ttl_ms = math.max(1, math.floor((new_next - now_wall) * 1000 + 0.5))
redis.call('PSETEX', key, ttl_ms, tostring(new_next))
return tostring(wait_s)
"""


class CrawlDelayExceededError(Exception):
    """Raised when queued Crawl-delay wait for an origin exceeds `max_wait_seconds`."""

    def __init__(self, domain: str, retry_after: float) -> None:
        self.domain = domain
        self.retry_after = max(0.001, float(retry_after))
        super().__init__(
            f"Crawl-delay queue wait ({self.retry_after:.1f}s) for '{domain}' exceeds limit; retry later."
        )


class DomainThrottle:
    """Enforces bounded per-domain Crawl-delay pacing across single, batch, and scheduled fetches."""

    def __init__(
        self,
        *,
        max_crawl_delay: float = 30.0,
        max_wait_seconds: float | None = None,
        max_size: int = 1000,
        redis_client: Any | None = None,
    ) -> None:
        self.max_crawl_delay = float(max_crawl_delay)
        self.max_wait_seconds = (
            float(max_wait_seconds)
            if max_wait_seconds is not None
            else max(0.0, self.max_crawl_delay)
        )
        self.max_size = max(1, int(max_size))
        self.redis_client = redis_client

        self._next_allowed: OrderedDict[str, float] = OrderedDict()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()

    @staticmethod
    def _normalize_domain(domain: str) -> str:
        return canonical_host(domain)

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

    def _reserve_memory_slot(self, domain_key: str, delay: float) -> tuple[float, float]:
        now = time.monotonic()
        next_slot = self._next_allowed.get(domain_key, 0.0)
        if now >= next_slot:
            wait_seconds = 0.0
            new_slot = now + delay
        else:
            wait_seconds = next_slot - now
            if wait_seconds > self.max_wait_seconds:
                raise CrawlDelayExceededError(domain_key, retry_after=wait_seconds)
            new_slot = next_slot + delay

        self._next_allowed[domain_key] = new_slot
        self._next_allowed.move_to_end(domain_key)
        while len(self._next_allowed) > self.max_size:
            self._next_allowed.popitem(last=False)
        return wait_seconds, new_slot

    def _refund_memory_slot_on_cancel(
        self,
        domain_key: str,
        reserved_slot: float,
        delay: float,
    ) -> None:
        current = self._next_allowed.get(domain_key)
        if current == reserved_slot:
            self._next_allowed[domain_key] = max(0.0, reserved_slot - delay)

    async def _reserve_redis_slot(self, domain_key: str, delay: float) -> float:
        assert self.redis_client is not None
        redis_key = f"throttle:v1:{domain_key}"
        now_wall = time.time()
        raw_wait = await self.redis_client.eval(
            _RESERVE_SLOT_LUA,
            1,
            redis_key,
            str(now_wall),
            str(delay),
            str(self.max_wait_seconds),
        )
        if isinstance(raw_wait, bytes):
            raw_wait = raw_wait.decode("utf-8", errors="ignore")
        parsed_wait = float(raw_wait)
        if parsed_wait < 0.0:
            raise CrawlDelayExceededError(domain_key, retry_after=-parsed_wait)
        return parsed_wait

    async def wait_if_needed(self, domain: str, crawl_delay: float | None) -> float:
        """Sleep until the next allowed request slot for `domain`, or raise if queue wait exceeds bound."""
        if crawl_delay is None or crawl_delay <= 0.0:
            return 0.0

        domain_key = self._normalize_domain(domain)
        if not domain_key:
            return 0.0

        delay = min(float(crawl_delay), self.max_crawl_delay)
        if delay <= 0.0:
            return 0.0

        reserved_mem_slot: float | None = None
        lock = self._get_lock(domain_key)
        async with lock:
            if self.redis_client is not None:
                try:
                    wait_seconds = await self._reserve_redis_slot(domain_key, delay)
                except CrawlDelayExceededError:
                    raise
                except (RedisError, OSError, RuntimeError, TypeError, ValueError) as exc:
                    logger.warning(
                        f"Redis domain throttle failed for {domain_key}, using memory slot: {exc}"
                    )
                    wait_seconds, reserved_mem_slot = self._reserve_memory_slot(domain_key, delay)
            else:
                wait_seconds, reserved_mem_slot = self._reserve_memory_slot(domain_key, delay)

        if wait_seconds > 0.0:
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                if reserved_mem_slot is not None:
                    self._refund_memory_slot_on_cancel(domain_key, reserved_mem_slot, delay)
                raise
        return wait_seconds

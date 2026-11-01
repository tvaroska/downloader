"""Rate limiting middleware for FastAPI using limits MovingWindowRateLimiter."""

from __future__ import annotations

import logging
import math
import re
import time
from collections.abc import Callable
from typing import Any, cast

import limits
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from limits.storage import MemoryStorage, MovingWindowSupport, Storage, storage_from_string
from limits.strategies import FixedWindowRateLimiter, MovingWindowRateLimiter, RateLimiter
from slowapi import Limiter
from starlette.middleware.base import BaseHTTPMiddleware

from .config import Settings, get_settings
from .models.responses import ErrorResponse

logger = logging.getLogger(__name__)

_SKIP_PATHS = frozenset({"/docs", "/redoc", "/openapi.json"})
_ACTIVE_MIDDLEWARES: list[RateLimitMiddleware] = []


def _make_strategy(storage: Storage) -> RateLimiter:
    if isinstance(storage, MovingWindowSupport):
        return MovingWindowRateLimiter(storage)
    return FixedWindowRateLimiter(storage)


def reset_rate_limiter(limiter: Limiter | None = None) -> None:
    """Reset in-memory rate-limit counters (called on lifespan startup for test isolation)."""
    if limiter is not None:
        storage = getattr(limiter, "_storage", None)
        if storage is not None and hasattr(storage, "reset"):
            try:
                storage.reset()
            except Exception:
                pass
    for mw in list(_ACTIVE_MIDDLEWARES):
        mw.reset()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces endpoint-specific rate limits and emits X-RateLimit-* headers."""

    def __init__(self, app: Any, limiter: Limiter) -> None:
        super().__init__(app)
        self.limiter = limiter
        self._init_settings = get_settings()
        self.settings = self._init_settings
        self.rate_limits = self._build_rate_limit_patterns(self.settings)

        self._fallback_memory_storage = MemoryStorage()
        self._fallback_strategy: RateLimiter = MovingWindowRateLimiter(
            self._fallback_memory_storage
        )
        self._storages: dict[str, tuple[Storage, RateLimiter]] = {}
        limiter_storage = getattr(limiter, "_storage", None)
        limiter_uri = getattr(limiter, "_storage_uri", None) or "memory://"
        if isinstance(limiter_storage, Storage):
            self._storages[str(limiter_uri)] = (limiter_storage, _make_strategy(limiter_storage))

        _ACTIVE_MIDDLEWARES.append(self)

    def reset(self) -> None:
        """Reset all in-memory storages held by this middleware."""
        self._fallback_memory_storage.reset()
        for storage, _ in list(self._storages.values()):
            if isinstance(storage, MemoryStorage) or hasattr(storage, "reset"):
                try:
                    storage.reset()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _active_settings(self) -> Settings:
        latest = get_settings()
        if latest is not self._init_settings:
            self._init_settings = latest
            self.settings = latest
        self.rate_limits = self._build_rate_limit_patterns(self.settings)
        return self.settings

    def _build_rate_limit_patterns(
        self, settings: Settings | None = None
    ) -> list[tuple[re.Pattern[str], str]]:
        cfg = settings if settings is not None else self.settings
        if not cfg.ratelimit.enabled:
            return []

        return [
            (re.compile(r"^/(health|metrics)(/.*)?$"), cfg.ratelimit.status_limit),
            (re.compile(r"^/(batch|jobs)(/.*)?$"), cfg.ratelimit.batch_limit),
            (re.compile(r"^/(schedules(/.*)?|)$"), cfg.ratelimit.default_limit),
            (re.compile(r"^/.+$"), cfg.ratelimit.download_limit),
        ]

    def _classify_path(self, path: str) -> tuple[str, str] | None:
        cfg = self._active_settings()
        if not cfg.ratelimit.enabled:
            return None

        if re.match(r"^/(health|metrics)(/.*)?$", path):
            return "status", cfg.ratelimit.status_limit
        if re.match(r"^/(batch|jobs)(/.*)?$", path):
            return "batch", cfg.ratelimit.batch_limit
        if re.match(r"^/(schedules(/.*)?|)$", path):
            return "default", cfg.ratelimit.default_limit
        if re.match(r"^/.+$", path):
            return "download", cfg.ratelimit.download_limit
        return "default", cfg.ratelimit.default_limit

    def _get_rate_limit_for_path(self, path: str) -> str | None:
        """Return the configured rate-limit string for `path`, or None when disabled."""
        classified = self._classify_path(path)
        return classified[1] if classified is not None else None

    def _get_strategy(self, cfg: Settings) -> RateLimiter:
        uri = cfg.ratelimit.storage_uri or cfg.redis.redis_uri or "memory://"
        cached = self._storages.get(uri)
        if cached is not None:
            return cached[1]
        try:
            storage = cast(Storage, storage_from_string(uri))
            strategy = _make_strategy(storage)
            self._storages[uri] = (storage, strategy)
            return strategy
        except Exception as exc:
            logger.warning(f"Failed to initialize rate-limit storage {uri}, using memory: {exc}")
            return self._fallback_strategy

    def _extract_client_key(self, request: Request) -> str:
        key_func = getattr(self.limiter, "_key_func", None)
        if callable(key_func):
            try:
                key = key_func(request)
                if key:
                    return str(key)
            except Exception:
                pass
        if request.client and request.client.host:
            return request.client.host
        return "127.0.0.1"

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        classified = self._classify_path(path)
        if classified is None:
            return await call_next(request)

        bucket, limit_str = classified
        cfg = self.settings
        item = limits.parse(limit_str)
        client_key = self._extract_client_key(request)
        strategy = self._get_strategy(cfg)

        try:
            allowed = strategy.hit(item, bucket, client_key)
            stats = strategy.get_window_stats(item, bucket, client_key)
        except Exception as exc:
            logger.warning(f"Rate-limit backend error ({exc}); falling back to in-memory window")
            allowed = self._fallback_strategy.hit(item, bucket, client_key)
            stats = self._fallback_strategy.get_window_stats(item, bucket, client_key)

        now = time.time()
        reset_epoch = max(int(math.ceil(stats.reset_time)), int(now))
        retry_after = max(1, int(math.ceil(stats.reset_time - now)))
        rl_headers: dict[str, str] = {}
        if cfg.ratelimit.headers_enabled:
            rl_headers = {
                "X-RateLimit-Limit": str(item.amount),
                "X-RateLimit-Remaining": str(max(0, int(stats.remaining))),
                "X-RateLimit-Reset": str(reset_epoch),
            }

        if not allowed:
            headers = {**rl_headers, "Retry-After": str(retry_after)}
            payload = {
                "detail": ErrorResponse(
                    error=f"Rate limit exceeded: {item}",
                    error_type="rate_limit_exceeded",
                ).model_dump()
            }
            return JSONResponse(status_code=429, content=payload, headers=headers)

        response: Response = await call_next(request)
        if rl_headers:
            for h_name, h_val in rl_headers.items():
                response.headers[h_name] = h_val
        return response

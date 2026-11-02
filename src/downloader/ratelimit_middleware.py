"""Rate limiting middleware for FastAPI using limits MovingWindowRateLimiter."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import re
import time
import weakref
from collections.abc import Callable
from typing import Any, cast

import limits
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from limits.storage import MemoryStorage, MovingWindowSupport, Storage, storage_from_string
from limits.strategies import FixedWindowRateLimiter, MovingWindowRateLimiter, RateLimiter
from slowapi import Limiter
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import extract_request_api_key, is_auth_enabled, verify_api_key
from .config import Settings, get_settings
from .models.responses import ErrorResponse

logger = logging.getLogger(__name__)

_SKIP_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_ACTIVE_MIDDLEWARES: weakref.WeakSet[RateLimitMiddleware] = weakref.WeakSet()


def _make_strategy(storage: Storage) -> RateLimiter:
    if isinstance(storage, MovingWindowSupport):
        return MovingWindowRateLimiter(storage)
    return FixedWindowRateLimiter(storage)


def _is_valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def _compile_trusted_networks(
    proxies: list[str],
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in proxies:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning(f"Ignoring invalid RATELIMIT_TRUSTED_PROXIES entry: {entry!r}")
    return tuple(networks)


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
        self.rate_limits: list[tuple[str, re.Pattern[str], str]] = self._build_rate_limit_patterns(
            self.settings
        )
        self._trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
            _compile_trusted_networks(self.settings.ratelimit.trusted_proxies)
        )

        self._fallback_memory_storage = MemoryStorage()
        self._fallback_strategy: RateLimiter = MovingWindowRateLimiter(
            self._fallback_memory_storage
        )
        self._storages: dict[str, tuple[Storage, RateLimiter]] = {}
        limiter_storage = getattr(limiter, "_storage", None)
        limiter_uri = getattr(limiter, "_storage_uri", None) or "memory://"
        if isinstance(limiter_storage, Storage):
            self._storages[str(limiter_uri)] = (limiter_storage, _make_strategy(limiter_storage))

        _ACTIVE_MIDDLEWARES.add(self)

    def reset(self) -> None:
        """Reset all in-memory storages held by this middleware."""
        self._fallback_memory_storage.reset()
        for storage, _ in list(self._storages.values()):
            if isinstance(storage, MemoryStorage) or hasattr(storage, "reset"):
                try:
                    storage.reset()  # type: ignore[attr-defined]
                except Exception:
                    pass

    def _active_settings(self, request: Request | None = None) -> Settings:
        app_obj = getattr(request, "app", None) if request is not None else None
        state_obj = getattr(app_obj, "state", None)
        candidate = getattr(state_obj, "settings", None)
        latest = candidate if isinstance(candidate, Settings) else get_settings()
        if latest is not self._init_settings:
            self._init_settings = latest
            self.settings = latest
            self.rate_limits = self._build_rate_limit_patterns(self.settings)
            self._trusted_networks = _compile_trusted_networks(
                self.settings.ratelimit.trusted_proxies
            )
        return self.settings

    def _build_rate_limit_patterns(
        self, settings: Settings | None = None
    ) -> list[tuple[str, re.Pattern[str], str]]:
        cfg = settings if settings is not None else self.settings
        if not cfg.ratelimit.enabled:
            return []

        return [
            ("status", re.compile(r"^/(health|metrics|jobs)(/.*)?$"), cfg.ratelimit.status_limit),
            ("batch", re.compile(r"^/batch(/.*)?$"), cfg.ratelimit.batch_limit),
            ("default", re.compile(r"^/(schedules(/.*)?|)$"), cfg.ratelimit.default_limit),
            ("download", re.compile(r"^/.+$"), cfg.ratelimit.download_limit),
        ]

    def _classify_path(self, path: str, request: Request | None = None) -> tuple[str, str] | None:
        cfg = self._active_settings(request)
        if not cfg.ratelimit.enabled:
            return None

        for bucket, pattern, limit_str in self.rate_limits:
            if pattern.match(path):
                return bucket, limit_str
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

    def _is_trusted_ip(self, ip_str: str) -> bool:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        return any(ip_obj in net for net in self._trusted_networks)

    def _resolve_client_ip(self, request: Request, cfg: Settings) -> str:
        _ = cfg
        peer_ip: str | None = None
        if request.client and request.client.host:
            peer_ip = request.client.host
        else:
            key_func = getattr(self.limiter, "_key_func", None)
            if callable(key_func):
                try:
                    key = key_func(request)
                    if key:
                        peer_ip = str(key)
                except Exception:
                    pass
        if not peer_ip:
            peer_ip = "127.0.0.1"

        if self._is_trusted_ip(peer_ip):
            xff = request.headers.get("x-forwarded-for")
            if xff:
                valid_hops = [h.strip() for h in xff.split(",") if _is_valid_ip(h.strip())]
                if valid_hops:
                    for hop in reversed(valid_hops):
                        if not self._is_trusted_ip(hop):
                            return hop
                    return valid_hops[0]

        return peer_ip

    def _resolve_bucket_and_key(
        self,
        request: Request,
        path: str,
        bucket: str,
        limit_str: str,
        cfg: Settings,
    ) -> tuple[str, str, str]:
        client_ip = self._resolve_client_ip(request, cfg)
        if path != "/health" and is_auth_enabled(cfg):
            token = extract_request_api_key(request)
            if token and verify_api_key(token, cfg):
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
                return bucket, limit_str, f"key:{digest}:{client_ip}"
            return "unauth", cfg.ratelimit.default_limit, f"ip:{client_ip}"
        return bucket, limit_str, f"ip:{client_ip}"

    def _extract_client_key(self, request: Request) -> str:
        return self._resolve_client_ip(request, self._active_settings(request))

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            return await call_next(request)

        classified = self._classify_path(path, request)
        if classified is None:
            return await call_next(request)

        bucket, limit_str = classified
        cfg = self.settings
        bucket, effective_limit_str, client_key = self._resolve_bucket_and_key(
            request, path, bucket, limit_str, cfg
        )
        item = limits.parse(effective_limit_str)
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

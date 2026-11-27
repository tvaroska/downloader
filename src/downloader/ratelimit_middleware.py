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
from typing import Any

import limits
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from limits.aio.storage import MemoryStorage, MovingWindowSupport, RedisStorage, Storage
from limits.aio.strategies import FixedWindowRateLimiter, MovingWindowRateLimiter, RateLimiter
from limits.errors import ConfigurationError, StorageError
from redis.exceptions import RedisError
from starlette.middleware.base import BaseHTTPMiddleware

from .auth import extract_request_api_key, is_auth_enabled, verify_api_key
from .config import Settings, get_settings
from .models.responses import ErrorResponse

logger = logging.getLogger(__name__)

_SKIP_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})
_ACTIVE_MIDDLEWARES: weakref.WeakSet[RateLimitMiddleware] = weakref.WeakSet()
_REDIS_SOCKET_TIMEOUT_SECONDS = 2.0


def _make_strategy(storage: Storage) -> RateLimiter:
    if isinstance(storage, MovingWindowSupport):
        return MovingWindowRateLimiter(storage)
    return FixedWindowRateLimiter(storage)


def _create_aio_storage(uri: str) -> Storage:
    """Instantiate an async `limits.aio` storage backend with bounded Redis socket timeouts."""
    if uri == "memory://" or uri.startswith("memory://"):
        return MemoryStorage()
    if uri.startswith(("redis://", "rediss://", "redis+unix://")):
        return RedisStorage(
            uri,
            implementation="redispy",
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    raise ValueError(f"Unsupported rate-limit storage URI: {uri}")


def _normalize_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Unwrap IPv4-mapped IPv6 addresses (`::ffff:x.y.z.w`) to canonical IPv4."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
        return ip_obj.ipv4_mapped
    return ip_obj


def _normalize_ip_str(raw_ip: str) -> str:
    try:
        return str(_normalize_ip(ipaddress.ip_address(raw_ip.strip("[]"))))
    except ValueError:
        return raw_ip


def _parse_xff_hop(raw_hop: str) -> str | None:
    """Parse one X-Forwarded-For hop, stripping optional `:port` and IPv4-mapped IPv6 prefix."""
    token = raw_hop.strip()
    if not token:
        return None
    if token.startswith("["):
        end = token.find("]")
        rest = token[end + 1 :] if end != -1 else ""
        if end == -1 or (rest and (not rest.startswith(":") or not rest[1:].isdigit())):
            return None
        candidate = token[1:end]
        ip_cls: type[ipaddress.IPv6Address] | None = ipaddress.IPv6Address
    elif token.count(":") == 1:
        host_part, port_part = token.split(":", 1)
        if not port_part.isdigit():
            return None
        candidate = host_part
        ip_cls = None
    else:
        candidate = token
        ip_cls = None
    try:
        ip_obj = ip_cls(candidate) if ip_cls is not None else ipaddress.ip_address(candidate)
        return str(_normalize_ip(ip_obj))
    except ValueError:
        return None


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


def reset_rate_limiter(limiter: Any = None) -> None:
    """Reset in-memory rate-limit counters on lifespan startup; never clear shared Redis."""
    if limiter is not None:
        storage = getattr(limiter, "_storage", None)
        storage_uri = str(getattr(limiter, "_storage_uri", None) or "memory://")
        reset_fn = getattr(storage, "reset", None)
        if storage is not None and storage_uri.startswith("memory://") and callable(reset_fn):
            try:
                reset_fn()
            except (OSError, RuntimeError, AttributeError, TypeError) as exc:
                logger.debug(f"Rate-limit limiter storage reset skipped: {exc}")
    for mw in list(_ACTIVE_MIDDLEWARES):
        mw.reset()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware enforcing endpoint-specific rate limits and `X-RateLimit-*` headers."""

    def __init__(self, app: Any, limiter: Any = None) -> None:
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

        _ACTIVE_MIDDLEWARES.add(self)

    def reset(self) -> None:
        """Reset in-memory rate-limit storages held by this middleware; never flush Redis."""
        self._fallback_memory_storage = MemoryStorage()
        self._fallback_strategy = MovingWindowRateLimiter(self._fallback_memory_storage)
        for uri, (storage, _) in list(self._storages.items()):
            if isinstance(storage, MemoryStorage):
                fresh = MemoryStorage()
                self._storages[uri] = (fresh, _make_strategy(fresh))

    def _active_settings(self, request: Request | None = None) -> Settings:
        app_obj = getattr(request, "app", None) if request is not None else self.app
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

    def _get_strategy(self, cfg: Settings) -> RateLimiter:
        uri = cfg.ratelimit.storage_uri or cfg.redis.redis_uri or "memory://"
        cached = self._storages.get(uri)
        if cached is not None:
            return cached[1]
        try:
            storage = _create_aio_storage(uri)
            strategy = _make_strategy(storage)
            self._storages[uri] = (storage, strategy)
            return strategy
        except (
            RedisError,
            ConfigurationError,
            OSError,
            RuntimeError,
            ValueError,
            ImportError,
        ) as exc:
            logger.warning(f"Failed to initialize rate-limit storage {uri}, using memory: {exc}")
            return self._fallback_strategy

    def _is_trusted_ip(self, ip_str: str) -> bool:
        try:
            ip_obj = _normalize_ip(ipaddress.ip_address(ip_str.strip("[]")))
        except ValueError:
            return False
        return any(ip_obj in net for net in self._trusted_networks)

    def _resolve_client_ip(self, request: Request) -> str:
        if not (request.client and request.client.host):
            # Never trust X-Forwarded-For when ASGI peer address is absent.
            key_func = getattr(self.limiter, "_key_func", None)
            if callable(key_func):
                try:
                    key = key_func(request)
                    if key:
                        return _normalize_ip_str(str(key))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.debug(
                        f"Rate-limit key_func fallback failed on clientless request: {exc}"
                    )
            return "unknown"

        peer_ip = _normalize_ip_str(request.client.host)
        if self._is_trusted_ip(peer_ip):
            xff = request.headers.get("x-forwarded-for")
            if xff:
                candidate = peer_ip
                for raw_hop in reversed(xff.split(",")):
                    hop = _parse_xff_hop(raw_hop)
                    if hop is None:
                        return candidate
                    if not self._is_trusted_ip(hop):
                        return hop
                    candidate = hop
                return candidate

        return peer_ip

    def _resolve_bucket_and_key(
        self,
        request: Request,
        bucket: str,
        limit_str: str,
    ) -> tuple[str, str, str]:
        cfg = self.settings
        client_ip = self._resolve_client_ip(request)
        if request.url.path != "/health" and is_auth_enabled(cfg):
            token = extract_request_api_key(request)
            if token and verify_api_key(token, cfg):
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
                return bucket, limit_str, f"key:{digest}:{client_ip}"
            return "unauth", cfg.ratelimit.default_limit, f"ip:{client_ip}"
        return bucket, limit_str, f"ip:{client_ip}"

    async def _evaluate_window(
        self,
        strategy: RateLimiter,
        item: limits.RateLimitItem,
        bucket: str,
        client_key: str,
    ) -> tuple[bool, Any]:
        try:
            allowed = await strategy.hit(item, bucket, client_key)
            stats = await strategy.get_window_stats(item, bucket, client_key)
            return allowed, stats
        except (RedisError, StorageError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(f"Rate-limit backend error ({exc}); falling back to in-memory window")
            allowed = await self._fallback_strategy.hit(item, bucket, client_key)
            stats = await self._fallback_strategy.get_window_stats(item, bucket, client_key)
            return allowed, stats

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        path = request.url.path
        if path in _SKIP_PATHS:
            skip_resp: Response = await call_next(request)
            return skip_resp

        classified = self._classify_path(path, request)
        if classified is None:
            unclassified_resp: Response = await call_next(request)
            return unclassified_resp

        bucket, limit_str = classified
        cfg = self.settings
        bucket, effective_limit_str, client_key = self._resolve_bucket_and_key(
            request, bucket, limit_str
        )
        item = limits.parse(effective_limit_str)
        strategy = self._get_strategy(cfg)
        allowed, stats = await self._evaluate_window(strategy, item, bucket, client_key)

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
        for h_name, h_val in rl_headers.items():
            response.headers[h_name] = h_val
        return response

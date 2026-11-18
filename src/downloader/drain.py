"""Graceful shutdown coordinator and connection-drain middleware."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import FrameType
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .models.responses import ErrorResponse

logger = logging.getLogger(__name__)

_DRAIN_EXEMPT_PATHS = frozenset({"/health"})


class DrainCoordinator:
    """Tracks in-flight requests and coordinates graceful shutdown draining."""

    def __init__(self) -> None:
        self._draining = False
        self._active_requests = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    @property
    def is_draining(self) -> bool:
        return self._draining

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def reset(self) -> None:
        """Reset coordinator state at lifespan startup."""
        self._draining = False
        self._active_requests = 0
        self._idle_event.set()

    def start_drain(self) -> None:
        """Stop accepting new work and begin draining active requests."""
        if not self._draining:
            self._draining = True
            logger.info(f"Entering connection drain mode (active_requests={self._active_requests})")

    @asynccontextmanager
    async def track_request(self) -> AsyncIterator[None]:
        """Track a single active HTTP request across its full response lifecycle."""
        self._active_requests += 1
        self._idle_event.clear()
        try:
            yield
        finally:
            self._active_requests = max(0, self._active_requests - 1)
            if self._active_requests == 0:
                self._idle_event.set()

    async def wait_until_drained(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for active requests to reach zero.

        Returns True if all requests drained cleanly before timeout, False otherwise.
        """
        if self._active_requests == 0:
            return True
        if timeout <= 0:
            return False
        try:
            await asyncio.wait_for(self._idle_event.wait(), timeout=timeout)
            logger.info("All in-flight HTTP requests drained cleanly")
            return True
        except TimeoutError:
            logger.warning(
                f"Connection drain timed out after {timeout:.1f}s "
                f"with {self._active_requests} active request(s) remaining"
            )
            return False


class DrainMiddleware(BaseHTTPMiddleware):
    """Rejects new work with HTTP 503 during shutdown while tracking in-flight requests."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        coordinator: DrainCoordinator | None = getattr(request.app.state, "drain_coordinator", None)
        if coordinator is None or request.url.path in _DRAIN_EXEMPT_PATHS:
            return await call_next(request)

        if coordinator.is_draining:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": ErrorResponse(
                        error="Service is shutting down and draining active connections.",
                        error_type="service_draining",
                    ).model_dump()
                },
                headers={"Connection": "close", "Retry-After": "5"},
            )

        async with coordinator.track_request():
            return await call_next(request)


def _restore_sigterm_handler(expected: Any, prev_handler: Any) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        if signal.getsignal(signal.SIGTERM) is expected:
            signal.signal(signal.SIGTERM, prev_handler)
    except (ValueError, OSError):
        pass


def install_sigterm_drain_handler(coordinator: DrainCoordinator) -> Callable[[], None]:
    """Install a SIGTERM handler that enters drain mode before chaining to the prior handler.

    Returns a zero-argument callback that restores the previous SIGTERM handler.
    No-ops safely when called off the main thread (e.g., TestClient portal threads).
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    try:
        prev_handler = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        return lambda: None

    def _on_sigterm(signum: int, frame: FrameType | None) -> None:
        coordinator.start_drain()
        if callable(prev_handler):
            prev_handler(signum, frame)
        elif prev_handler == signal.SIG_DFL:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        return lambda: None

    return lambda: _restore_sigterm_handler(_on_sigterm, prev_handler)

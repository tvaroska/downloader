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
    """Tracks in-flight requests and coordinates pre-stop readiness and graceful connection drain."""

    def __init__(self) -> None:
        self._shutting_down = False
        self._draining = False
        self._active_requests = 0
        self._idle_event = asyncio.Event()
        self._idle_event.set()

    @property
    def is_shutting_down(self) -> bool:
        """Return True during both the pre-stop readiness window and active connection drain."""
        return self._shutting_down or self._draining

    @property
    def is_draining(self) -> bool:
        """Return True once the pre-stop delay has elapsed and new requests should be rejected."""
        return self._draining

    @property
    def active_requests(self) -> int:
        return self._active_requests

    def reset(self) -> None:
        """Reset coordinator state at lifespan startup."""
        self._shutting_down = False
        self._draining = False
        self._active_requests = 0
        self._idle_event.set()

    def begin_pre_stop(self) -> None:
        """Fail `/health` readiness probes (`503`) while still servicing routable live requests."""
        if not self._shutting_down:
            self._shutting_down = True
            logger.info(
                f"Entering pre-stop readiness window (active_requests={self._active_requests})"
            )

    def start_drain(self) -> None:
        """Stop accepting new work and begin draining active requests."""
        self._shutting_down = True
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
            exempt_resp: Response = await call_next(request)
            return exempt_resp

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
            tracked_resp: Response = await call_next(request)
            return tracked_resp


def _restore_sigterm_handler(expected: Any, prev_handler: Any) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        if signal.getsignal(signal.SIGTERM) is expected:
            signal.signal(signal.SIGTERM, prev_handler)
    except (ValueError, OSError):
        pass


def _get_running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _dispatch_prev_sigterm(prev_handler: Any, signum: int, frame: FrameType | None) -> None:
    if callable(prev_handler):
        prev_handler(signum, frame)
    elif prev_handler == signal.SIG_DFL:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


class _SigtermDrainChain:
    """Holds drain-mode pre-stop timer state and chains to the prior SIGTERM handler."""

    def __init__(
        self,
        coordinator: DrainCoordinator,
        prev_handler: Any,
        pre_stop_delay: float,
        loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        self._coordinator = coordinator
        self._prev_handler = prev_handler
        self._pre_stop_delay = pre_stop_delay
        self._loop = loop
        self._handle: asyncio.TimerHandle | None = None
        self._chained = False

    def _invoke_prev(self, signum: int, frame: FrameType | None) -> None:
        if self._chained:
            return
        self._chained = True
        self._handle = None
        self._coordinator.start_drain()
        _dispatch_prev_sigterm(self._prev_handler, signum, frame)

    def handle_signal(self, signum: int, frame: FrameType | None) -> None:
        if self._pre_stop_delay > 0 and self._loop is not None and self._loop.is_running():
            self._coordinator.begin_pre_stop()
            if self._handle is None and not self._chained:
                logger.info(
                    f"Holding /health at 503 for {self._pre_stop_delay:.2f}s pre-stop delay "
                    "while servicing routable traffic"
                )
                self._handle = self._loop.call_later(
                    self._pre_stop_delay, self._invoke_prev, signum, None
                )
            return
        self._coordinator.start_drain()
        self._invoke_prev(signum, frame)

    def restore(self, registered_handler: Any) -> None:
        if self._handle is not None:
            self._handle.cancel()
        _restore_sigterm_handler(registered_handler, self._prev_handler)


def install_sigterm_drain_handler(
    coordinator: DrainCoordinator,
    *,
    pre_stop_delay: float = 0.0,
) -> Callable[[], None]:
    """Install a SIGTERM handler that enters drain mode before chaining to the prior handler.

    When `pre_stop_delay > 0` and an event loop is running, holds the server open in drain mode
    so readiness probes observe `/health` returning `503` before forwarding SIGTERM to the server.
    Returns a zero-argument callback that cancels any pending delay and restores the prior handler.
    No-ops safely when called off the main thread (e.g., TestClient portal threads).
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    try:
        prev_handler = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        return lambda: None

    chain = _SigtermDrainChain(
        coordinator,
        prev_handler,
        pre_stop_delay,
        _get_running_loop_or_none(),
    )
    handler = chain.handle_signal
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):
        return lambda: None
    return lambda: chain.restore(handler)

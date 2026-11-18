"""FastAPI dependency injection providers for Ports, Adapters and Contracts."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import Depends, Request

from .config import Settings, get_settings
from .crawling import DomainThrottle, RobotsChecker
from .http_client import HTTPClient
from .pdf_generator import PlaywrightPDFGenerator
from .ports import (
    BrowserRenderer,
    JobStore,
    LocalBrowserRenderer,
    RemoteBrowserRenderer,
    WorkQueue,
)
from .scheduler import ExecutionStorage, SchedulerService
from .validation import EgressPolicy
from .worker_queue import WorkerQueue

logger = logging.getLogger(__name__)


def build_browser_renderer(
    settings: Settings,
    *,
    pdf_semaphore: asyncio.Semaphore | None = None,
    work_queue: WorkQueue | None = None,
) -> BrowserRenderer:
    """Select the remote or local `BrowserRenderer` adapter from runtime configuration."""
    egress_policy = EgressPolicy(settings)
    max_bytes = settings.content.max_download_size
    if (
        settings.worker.deployment_mode == "api"
        and settings.worker.remote_browser_enabled
        and work_queue is not None
    ):
        return RemoteBrowserRenderer(
            work_queue,
            timeout=settings.worker.remote_browser_timeout,
            egress_policy=egress_policy,
            max_output_bytes=max_bytes,
        )
    return LocalBrowserRenderer(
        pdf_semaphore,
        egress_policy=egress_policy,
        max_output_bytes=max_bytes,
    )


async def get_http_client(request: Request) -> HTTPClient:
    """Return the lifespan-managed `HTTPClient` from `app.state`."""
    if not hasattr(request.app.state, "http_client"):
        raise RuntimeError("HTTP client not initialized. Check app lifespan configuration.")
    return request.app.state.http_client


async def get_job_manager_dependency(request: Request) -> JobStore | None:
    """Return the configured `JobStore` (`JobManager` or `InMemoryJobStore`) or `None`."""
    return getattr(request.app.state, "job_manager", None)


async def get_pdf_generator_dependency(request: Request) -> PlaywrightPDFGenerator | None:
    """Return the configured `PlaywrightPDFGenerator` from `app.state`."""
    if not hasattr(request.app.state, "pdf_generator"):
        raise RuntimeError("PDF generator not initialized. Check app lifespan configuration.")
    return request.app.state.pdf_generator


def get_pdf_semaphore(request: Request) -> asyncio.Semaphore:
    """Return the shared browser concurrency semaphore from `app.state`."""
    if not hasattr(request.app.state, "pdf_semaphore"):
        raise RuntimeError("PDF semaphore not initialized. Check app lifespan configuration.")
    return request.app.state.pdf_semaphore


def get_batch_semaphore(request: Request) -> asyncio.Semaphore:
    """Return the batch concurrency semaphore from `app.state`."""
    if not hasattr(request.app.state, "batch_semaphore"):
        raise RuntimeError("Batch semaphore not initialized. Check app lifespan configuration.")
    return request.app.state.batch_semaphore


def get_settings_dependency() -> Settings:
    """Return the global `Settings` instance."""
    return get_settings()


async def get_scheduler_dependency(request: Request) -> SchedulerService | None:
    """Return `SchedulerService` from `app.state` when Redis is configured."""
    return getattr(request.app.state, "scheduler", None)


async def get_execution_storage_dependency(request: Request) -> ExecutionStorage | None:
    """Return `ExecutionStorage` from `app.state` when Redis is configured."""
    return getattr(request.app.state, "execution_storage", None)


async def get_robots_checker(request: Request) -> RobotsChecker:
    """Return or lazily initialize `RobotsChecker` bound to `app.state.settings`."""
    checker: RobotsChecker | None = getattr(request.app.state, "robots_checker", None)
    if checker is None:
        active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
        cfg = active_settings.crawling
        job_manager: Any = getattr(request.app.state, "job_manager", None)
        redis_client = getattr(job_manager, "redis_client", None)
        checker = RobotsChecker(
            user_agent=cfg.user_agent,
            robots_cache_ttl=cfg.robots_cache_ttl,
            robots_cache_max_size=cfg.robots_cache_max_size,
            robots_fetch_timeout=cfg.robots_fetch_timeout,
            max_crawl_delay=cfg.max_crawl_delay,
            redis_client=redis_client,
            egress_policy=EgressPolicy(active_settings),
        )
        request.app.state.robots_checker = checker
    return checker


async def get_domain_throttle(request: Request) -> DomainThrottle:
    """Return or lazily initialize `DomainThrottle` bound to `app.state.settings`."""
    throttle: DomainThrottle | None = getattr(request.app.state, "domain_throttle", None)
    if throttle is None:
        active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
        cfg = active_settings.crawling
        job_manager: Any = getattr(request.app.state, "job_manager", None)
        redis_client = getattr(job_manager, "redis_client", None)
        throttle = DomainThrottle(
            max_crawl_delay=cfg.max_crawl_delay,
            max_size=cfg.robots_cache_max_size,
            redis_client=redis_client,
        )
        request.app.state.domain_throttle = throttle
    return throttle


async def get_worker_queue_dependency(
    request: Request,
    job_manager: Annotated[JobStore | None, Depends(get_job_manager_dependency)],
) -> WorkQueue | None:
    """Return the active `WorkQueue` when Redis or an injected queue is present."""
    queue: WorkQueue | None = getattr(request.app.state, "worker_queue", None)
    if queue is not None:
        return queue
    redis_client = getattr(job_manager, "redis_client", None)
    if redis_client is not None:
        return WorkerQueue(redis_client)
    return None


async def get_browser_renderer(
    request: Request,
    pdf_semaphore: Annotated[asyncio.Semaphore, Depends(get_pdf_semaphore)],
    work_queue: Annotated[WorkQueue | None, Depends(get_worker_queue_dependency)],
) -> BrowserRenderer:
    """Return the active `BrowserRenderer` chosen for this application state."""
    active_settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    stored: BrowserRenderer | None = getattr(request.app.state, "browser_renderer", None)
    stored_mode = getattr(request.app.state, "_browser_renderer_mode", None)
    state_sem = getattr(request.app.state, "pdf_semaphore", None)
    state_queue = getattr(request.app.state, "worker_queue", None)
    current_mode = (
        active_settings.worker.deployment_mode,
        active_settings.worker.remote_browser_enabled,
        active_settings.worker.remote_browser_timeout,
        active_settings.content.max_download_size,
    )
    if (
        stored is not None
        and stored_mode == current_mode
        and pdf_semaphore is state_sem
        and work_queue is state_queue
    ):
        return stored

    renderer = build_browser_renderer(
        active_settings,
        pdf_semaphore=pdf_semaphore,
        work_queue=work_queue,
    )
    if pdf_semaphore is state_sem and work_queue is state_queue:
        request.app.state.browser_renderer = renderer
        request.app.state._browser_renderer_mode = current_mode
    return renderer


SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]
HTTPClientDep = Annotated[HTTPClient, Depends(get_http_client)]
JobStoreDep = Annotated[JobStore | None, Depends(get_job_manager_dependency)]
JobManagerDep = JobStoreDep
PDFGeneratorDep = Annotated[PlaywrightPDFGenerator | None, Depends(get_pdf_generator_dependency)]
PDFSemaphoreDep = Annotated[asyncio.Semaphore, Depends(get_pdf_semaphore)]
BatchSemaphoreDep = Annotated[asyncio.Semaphore, Depends(get_batch_semaphore)]
SchedulerDep = Annotated[SchedulerService | None, Depends(get_scheduler_dependency)]
ExecutionStorageDep = Annotated[ExecutionStorage | None, Depends(get_execution_storage_dependency)]
RobotsCheckerDep = Annotated[RobotsChecker, Depends(get_robots_checker)]
DomainThrottleDep = Annotated[DomainThrottle, Depends(get_domain_throttle)]
WorkQueueDep = Annotated[WorkQueue | None, Depends(get_worker_queue_dependency)]
WorkerQueueDep = WorkQueueDep
BrowserRendererDep = Annotated[BrowserRenderer, Depends(get_browser_renderer)]

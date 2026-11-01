"""Dependency injection providers for FastAPI.

This module provides dependency injection functions that replace global singletons
with proper FastAPI dependency management. This improves testability and follows
best practices for stateful resource management.
"""

import asyncio
import logging
from typing import Annotated

from fastapi import Depends, Request
from slowapi import Limiter

from .config import Settings, get_settings
from .crawling import DomainThrottle, RobotsChecker
from .http_client import HTTPClient
from .job_manager import JobManager
from .pdf_generator import PlaywrightPDFGenerator
from .scheduler import ExecutionStorage, SchedulerService

logger = logging.getLogger(__name__)


# HTTP Client Dependency
async def get_http_client(request: Request) -> HTTPClient:
    """
    Get HTTP client instance from app state.

    The client is initialized during app lifespan and stored in app.state.
    This ensures proper connection pooling and resource management.

    Args:
        request: FastAPI request containing app state

    Returns:
        HTTPClient instance

    Raises:
        RuntimeError: If HTTP client not initialized
    """
    if not hasattr(request.app.state, "http_client"):
        raise RuntimeError("HTTP client not initialized. Check app lifespan configuration.")
    return request.app.state.http_client


# Job Manager Dependency
async def get_job_manager_dependency(request: Request) -> JobManager | None:
    """
    Get job manager instance from app state.

    The job manager is initialized during app lifespan if Redis is configured.
    Returns None if Redis is not available.

    Args:
        request: FastAPI request containing app state

    Returns:
        JobManager instance or None if not configured
    """
    return getattr(request.app.state, "job_manager", None)


# PDF Generator Dependency
async def get_pdf_generator_dependency(
    request: Request,
) -> PlaywrightPDFGenerator | None:
    """
    Get PDF generator instance from app state.

    The PDF generator is initialized during app lifespan with browser pool.

    Args:
        request: FastAPI request containing app state

    Returns:
        PDFGenerator instance or None if not available

    Raises:
        RuntimeError: If PDF generator not initialized
    """
    if not hasattr(request.app.state, "pdf_generator"):
        raise RuntimeError("PDF generator not initialized. Check app lifespan configuration.")
    return request.app.state.pdf_generator


# Semaphore Dependencies
def get_pdf_semaphore(request: Request) -> asyncio.Semaphore:
    """
    Get PDF generation semaphore from app state.

    Controls concurrency for PDF generation to prevent resource exhaustion.

    Args:
        request: FastAPI request containing app state

    Returns:
        Semaphore for PDF generation concurrency control
    """
    if not hasattr(request.app.state, "pdf_semaphore"):
        raise RuntimeError("PDF semaphore not initialized. Check app lifespan configuration.")
    return request.app.state.pdf_semaphore


def get_batch_semaphore(request: Request) -> asyncio.Semaphore:
    """
    Get batch processing semaphore from app state.

    Controls concurrency for batch operations to prevent overload.

    Args:
        request: FastAPI request containing app state

    Returns:
        Semaphore for batch processing concurrency control
    """
    if not hasattr(request.app.state, "batch_semaphore"):
        raise RuntimeError("Batch semaphore not initialized. Check app lifespan configuration.")
    return request.app.state.batch_semaphore


# Settings Dependency
def get_settings_dependency() -> Settings:
    """
    Get application settings.

    Returns the global settings instance configured from environment variables.
    This is a simple dependency that doesn't require request context.

    Returns:
        Settings instance
    """
    return get_settings()


# Rate Limiter Dependency
def get_rate_limiter(request: Request) -> Limiter:
    """
    Get rate limiter instance from app state.

    The limiter is initialized during app startup with configured limits.

    Args:
        request: FastAPI request containing app state

    Returns:
        Limiter instance

    Raises:
        RuntimeError: If limiter not initialized
    """
    if not hasattr(request.app.state, "limiter"):
        raise RuntimeError("Rate limiter not initialized. Check app configuration.")
    return request.app.state.limiter


# Scheduler Dependency
async def get_scheduler_dependency(request: Request) -> SchedulerService | None:
    """
    Get scheduler service instance from app state.

    The scheduler is initialized during app lifespan if Redis is configured.
    Returns None if Redis is not available.

    Args:
        request: FastAPI request containing app state

    Returns:
        SchedulerService instance or None if not configured
    """
    return getattr(request.app.state, "scheduler", None)


# Execution Storage Dependency
async def get_execution_storage_dependency(request: Request) -> ExecutionStorage | None:
    """
    Get execution storage instance from app state.

    The execution storage is initialized during app lifespan if Redis is configured.
    Returns None if Redis is not available.

    Args:
        request: FastAPI request containing app state

    Returns:
        ExecutionStorage instance or None if not configured
    """
    return getattr(request.app.state, "execution_storage", None)


async def get_robots_checker(request: Request) -> RobotsChecker:
    """Get or lazily initialize the RobotsChecker instance from app state."""
    checker: RobotsChecker | None = getattr(request.app.state, "robots_checker", None)
    if checker is None:
        cfg = get_settings().crawling
        job_manager: JobManager | None = getattr(request.app.state, "job_manager", None)
        redis_client = job_manager.redis_client if job_manager is not None else None
        checker = RobotsChecker(
            user_agent=cfg.user_agent,
            robots_cache_ttl=cfg.robots_cache_ttl,
            robots_cache_max_size=cfg.robots_cache_max_size,
            robots_fetch_timeout=cfg.robots_fetch_timeout,
            max_crawl_delay=cfg.max_crawl_delay,
            redis_client=redis_client,
        )
        request.app.state.robots_checker = checker
    return checker


async def get_domain_throttle(request: Request) -> DomainThrottle:
    """Get or lazily initialize the DomainThrottle instance from app state."""
    throttle: DomainThrottle | None = getattr(request.app.state, "domain_throttle", None)
    if throttle is None:
        cfg = get_settings().crawling
        job_manager: JobManager | None = getattr(request.app.state, "job_manager", None)
        redis_client = job_manager.redis_client if job_manager is not None else None
        throttle = DomainThrottle(
            max_crawl_delay=cfg.max_crawl_delay,
            max_size=cfg.robots_cache_max_size,
            redis_client=redis_client,
        )
        request.app.state.domain_throttle = throttle
    return throttle


# Type aliases for cleaner route signatures
SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]
HTTPClientDep = Annotated[HTTPClient, Depends(get_http_client)]
JobManagerDep = Annotated[JobManager | None, Depends(get_job_manager_dependency)]
PDFGeneratorDep = Annotated[PlaywrightPDFGenerator | None, Depends(get_pdf_generator_dependency)]
PDFSemaphoreDep = Annotated[asyncio.Semaphore, Depends(get_pdf_semaphore)]
BatchSemaphoreDep = Annotated[asyncio.Semaphore, Depends(get_batch_semaphore)]
RateLimiterDep = Annotated[Limiter, Depends(get_rate_limiter)]
SchedulerDep = Annotated[SchedulerService | None, Depends(get_scheduler_dependency)]
ExecutionStorageDep = Annotated[ExecutionStorage | None, Depends(get_execution_storage_dependency)]
RobotsCheckerDep = Annotated[RobotsChecker, Depends(get_robots_checker)]
DomainThrottleDep = Annotated[DomainThrottle, Depends(get_domain_throttle)]

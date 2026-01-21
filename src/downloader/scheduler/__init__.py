"""Scheduler module for scheduled job management.

This module provides APScheduler integration for scheduling recurring
download jobs with Redis-backed persistence.
"""

from .service import SchedulerService, get_scheduler_service

__all__ = ["SchedulerService", "get_scheduler_service"]

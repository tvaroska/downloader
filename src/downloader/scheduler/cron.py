"""APScheduler trigger factory for validated 5-field UNIX cron expressions."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING

from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from ..models.schedule import _translate_unix_dow_field, validate_unix_crontab

if TYPE_CHECKING:
    from apscheduler.triggers.base import BaseTrigger


def cron_trigger_from_unix_crontab(expr: str) -> BaseTrigger:
    """Build a UTC APScheduler trigger from a 5-field UNIX crontab (0/7=Sun..6=Sat, DOM|DOW OR)."""
    minute, hour, dom, month, dow = validate_unix_crontab(expr)
    translated_dow = _translate_unix_dow_field(dow)
    if dom != "*" and dow != "*":
        dom_trigger = CronTrigger.from_crontab(f"{minute} {hour} {dom} {month} *", timezone=UTC)
        dow_trigger = CronTrigger.from_crontab(
            f"{minute} {hour} * {month} {translated_dow}",
            timezone=UTC,
        )
        return OrTrigger([dom_trigger, dow_trigger])

    return CronTrigger.from_crontab(
        f"{minute} {hour} {dom} {month} {translated_dow}",
        timezone=UTC,
    )

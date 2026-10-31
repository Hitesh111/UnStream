"""Automation package."""

from fiiolib.automation.scheduler import (
    LAUNCHD_LABEL,
    JobOutcome,
    Scheduler,
    SchedulerError,
    parse_schedule,
)

__all__ = ["LAUNCHD_LABEL", "JobOutcome", "Scheduler", "SchedulerError", "parse_schedule"]

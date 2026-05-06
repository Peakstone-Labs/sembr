"""APScheduler job lifecycle management for per-intent scan jobs.

D15: coalesce=True, max_instances=1, replace_existing=True — project-wide APScheduler convention.
D16: job ID = f"matcher-intent-{intent_id}" — stable, enables replace_existing-based reregister.
D8:  EventSchedule intents skip APScheduler registration (event-driven path).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sembr.vector_store.intents import ALIAS_NAME as _INTENTS_ALIAS

if TYPE_CHECKING:
    from sembr.models import CronSchedule, EventSchedule, Intent

logger = logging.getLogger(__name__)


def _job_id(intent_id: int) -> str:
    return f"matcher-intent-{intent_id}"


def _build_cron_trigger(schedule: "CronSchedule", timezone_str: str) -> CronTrigger:
    # Pass timezone as a string so APScheduler converts it to pytz internally.
    # Passing a ZoneInfo object causes type mismatches in APScheduler 3.x's
    # get_due_jobs() sorted-list comparison (ZoneInfo vs pytz datetimes),
    # which silently prevents the job from ever firing.
    if schedule.preset == "hourly":
        return CronTrigger(minute=schedule.minute, timezone=timezone_str)
    elif schedule.preset == "daily":
        return CronTrigger(hour=schedule.hour, minute=schedule.minute, timezone=timezone_str)
    else:  # weekly
        return CronTrigger(
            day_of_week=schedule.weekday,
            hour=schedule.hour,
            minute=schedule.minute,
            timezone=timezone_str,
        )


def register_intent_job(
    scheduler: AsyncIOScheduler,
    intent: "Intent",
    app,
    *,
    fire_immediately: bool = False,
) -> None:
    # Lazy import prevents circular dependency: jobs ← scan ← match_seen/intents/callback
    from sembr.matcher.scan import run_intent_scan  # noqa: PLC0415
    from sembr.models import CronSchedule, EventSchedule  # noqa: PLC0415

    schedule = intent.schedule

    # D8: event-mode intents are triggered by ingestion events, not APScheduler ticks
    if isinstance(schedule, EventSchedule):
        logger.debug("skipping APScheduler registration for event-mode intent_id=%d", intent.id)
        return

    if isinstance(schedule, CronSchedule):
        trigger = _build_cron_trigger(schedule, intent.timezone)
    else:
        raise NotImplementedError(f"unknown schedule mode: {schedule.mode!r}")

    # Compute expected next fire time directly from the trigger before add_job,
    # because job.next_run_time is None when the scheduler hasn't started yet.
    expected_next = trigger.get_next_fire_time(None, datetime.now(timezone.utc))

    scheduler.add_job(
        run_intent_scan,
        trigger=trigger,
        id=_job_id(intent.id),
        args=[intent.id, app],
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        misfire_grace_time=None,  # never skip due to late wakeup
    )
    # Do NOT pass next_run_time=None — APScheduler treats explicit None as
    # "paused", and the job will never fire. Omitting it lets _real_add_job
    # compute the first fire time from the trigger.
    logger.info(
        "registered matcher job intent_id=%d preset=%s hour=%d minute=%d tz=%s next_run=%s",
        intent.id,
        schedule.preset,
        schedule.hour,
        schedule.minute,
        intent.timezone,
        expected_next,
    )


def unregister_intent_job(scheduler: AsyncIOScheduler, intent_id: int) -> None:
    try:
        scheduler.remove_job(_job_id(intent_id))
        logger.debug("unregistered matcher job intent_id=%d", intent_id)
    except JobLookupError:
        # Expected when intent was created while disabled or job already removed
        logger.debug("matcher job intent_id=%d already absent", intent_id)


def reregister_intent_job(scheduler: AsyncIOScheduler, intent: "Intent", app) -> None:
    """Replace an existing job with updated trigger/args (D4, D5)."""
    register_intent_job(scheduler, intent, app)


async def register_all_enabled(
    scheduler: AsyncIOScheduler,
    intents: list["Intent"],
    app,
    qdrant_client,
) -> None:
    """Register jobs for all enabled intents at startup (D18).

    Checks Qdrant vector existence before registering: a partial DELETE failure
    leaves a vector-less intent row in SQLite. Re-registering such a job produces
    an infinite stream of "no vector in Qdrant" warnings. Skipping it here
    surfaces the inconsistency once at startup (as an ERROR) and stays quiet.
    """
    registered = 0
    for intent in intents:
        points = await qdrant_client.retrieve(
            collection_name=_INTENTS_ALIAS,
            ids=[intent.id],
            with_vectors=False,
        )
        if not points:
            logger.error(
                "intent_id=%d has no Qdrant vector at startup; skipping job registration. "
                "Disable or DELETE+POST this intent to resolve.",
                intent.id,
            )
            continue
        register_intent_job(scheduler, intent, app)
        registered += 1
    if intents:
        logger.info(
            "registered %d/%d matcher jobs for enabled intents on startup",
            registered,
            len(intents),
        )

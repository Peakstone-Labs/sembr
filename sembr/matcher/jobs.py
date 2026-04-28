"""APScheduler job lifecycle management for per-intent scan jobs.

D1:  each intent gets its own IntervalTrigger job (not a single global job).
D2:  start_date = first_scan_at or now (None → immediate first fire).
D15: coalesce=True, max_instances=1, replace_existing=True — project-wide APScheduler convention.
D16: job ID = f"matcher-intent-{intent_id}" — stable, enables replace_existing-based reregister.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

if TYPE_CHECKING:
    from sembr.models import Intent

logger = logging.getLogger(__name__)


def _job_id(intent_id: int) -> str:
    return f"matcher-intent-{intent_id}"


def register_intent_job(
    scheduler: AsyncIOScheduler,
    intent: "Intent",
    app,
    *,
    fire_immediately: bool = False,
) -> None:
    # Lazy import prevents circular dependency: jobs ← scan ← match_seen/intents/callback
    from sembr.matcher.scan import run_intent_scan  # noqa: PLC0415

    start_date: datetime | None = intent.first_scan_at
    if start_date is not None:
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if start_date < datetime.now(timezone.utc):
            # D2: past first_scan_at → schedule immediate first tick rather than
            # waiting for the next computed slot (start_date + n*interval ≥ now)
            logger.info(
                "intent_id=%d first_scan_at is in the past; scheduling immediate first tick",
                intent.id,
            )
            start_date = None

    # APScheduler IntervalTrigger computes ceil(elapsed/interval) for the first fire time,
    # so even start_date=now yields next_fire = now + interval for large intervals (e.g. 86400s).
    # next_run_time overrides the trigger for the first tick only; subsequent ticks use the trigger.
    next_run_time = datetime.now(timezone.utc) if fire_immediately else None

    scheduler.add_job(
        run_intent_scan,
        trigger=IntervalTrigger(seconds=intent.scan_interval_seconds, start_date=start_date),
        id=_job_id(intent.id),
        args=[intent.id, app],
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        next_run_time=next_run_time,
    )
    logger.debug(
        "registered matcher job intent_id=%d interval=%ds",
        intent.id,
        intent.scan_interval_seconds,
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
            collection_name="intents_current",
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

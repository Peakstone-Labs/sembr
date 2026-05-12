"""Dead-articles TTL job: prune ``dead_articles`` rows older than
``settings.dead_articles_retention_days``.

Independent of Qdrant retention: dead rows are forensic state for post-mortem
of embedder failures, not vector-store lifecycle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from time import monotonic

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import transaction

logger = logging.getLogger(__name__)


async def _run_dead_ttl(settings: Settings) -> None:
    started_at = monotonic()
    cutoff_iso = (
        datetime.now(timezone.utc) - timedelta(days=settings.dead_articles_retention_days)
    ).isoformat()
    deleted = 0
    try:
        async with transaction() as txn:
            await txn.execute("DELETE FROM dead_articles WHERE failed_at < ?", (cutoff_iso,))
            # SELECT changes() must run inside the same txn so a concurrent
            # writer can't slip its rowcount in between COMMIT and the read
            # (memory: feedback_sqlite_pragmas#3).
            async with txn.execute("SELECT changes()") as cur:
                deleted = (await cur.fetchone())[0]
    except Exception:
        logger.warning("dead_ttl run failed", exc_info=True)
        return
    elapsed_ms = int((monotonic() - started_at) * 1000)
    logger.info(
        "dead_ttl run: cutoff=%s deleted=%d elapsed_ms=%d interval_hours=%d",
        cutoff_iso,
        deleted,
        elapsed_ms,
        settings.maintenance_interval_hours,
    )


def add_dead_ttl_job(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """Register the dead-articles TTL job with a 25-minute startup offset."""
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        _run_dead_ttl,
        trigger=IntervalTrigger(
            hours=settings.maintenance_interval_hours,
            start_date=now + timedelta(minutes=25),
        ),
        id="maintenance_dead_ttl",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        args=[settings],
    )

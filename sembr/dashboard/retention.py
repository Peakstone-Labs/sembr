"""Hourly APScheduler job pruning event log rows beyond age / per-feed cap (D9 / F1).

Two predicates, applied as a union:
  (a) DELETE rows where started_at < now - retention_days
  (b) For each feed_id, keep newest N rows by id; delete older ones (FIFO)

Embed log only enforces (a) — there's a single embedder, so a global age cap is enough.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import transaction

logger = logging.getLogger(__name__)


async def _prune_logs(settings: Settings) -> None:
    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=settings.dashboard_log_retention_days)
    ).isoformat()
    max_per_feed = settings.dashboard_log_max_per_feed
    try:
        async with transaction() as conn:
            await conn.execute(
                "DELETE FROM feed_fetch_log WHERE started_at < ?", (cutoff,)
            )
            await conn.execute(
                "DELETE FROM embed_call_log WHERE started_at < ?", (cutoff,)
            )
            # Per-feed FIFO cap. Correlated subquery counts how many newer rows exist
            # per feed_id; rows with >= max_per_feed newer siblings are dropped.
            # id is monotonic (AUTOINCREMENT) so f2.id > f1.id ⇔ f2 is newer.
            await conn.execute(
                "DELETE FROM feed_fetch_log WHERE id IN ("
                "  SELECT f1.id FROM feed_fetch_log f1 "
                "  WHERE (SELECT COUNT(*) FROM feed_fetch_log f2 "
                "         WHERE f2.feed_id = f1.feed_id AND f2.id > f1.id) >= ?"
                ")",
                (max_per_feed,),
            )
    except Exception:
        logger.warning("dashboard log retention prune failed", exc_info=True)


def add_log_retention_job(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """Register the hourly retention job. Idempotent via replace_existing=True."""
    scheduler.add_job(
        _prune_logs,
        trigger=IntervalTrigger(hours=1),
        id="dashboard_log_retention",
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
        replace_existing=True,
        args=[settings],
    )

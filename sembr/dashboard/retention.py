# SPDX-License-Identifier: Apache-2.0
"""Hourly APScheduler job pruning event log rows beyond age / per-feed cap.

Two predicates, applied as a union:
  (a) DELETE rows where started_at < now - retention_days
  (b) For each feed_id, keep newest N rows by id; delete older ones (FIFO)

Embed log only enforces (a) — there's a single embedder, so a global age cap is enough.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import transaction

logger = logging.getLogger(__name__)


async def _prune_logs(settings: Settings) -> None:
    cutoff = (datetime.now(UTC) - timedelta(days=settings.dashboard_log_retention_days)).isoformat()
    max_per_feed = settings.dashboard_log_max_per_feed
    try:
        async with transaction() as conn:
            await conn.execute("DELETE FROM feed_fetch_log WHERE started_at < ?", (cutoff,))
            await conn.execute("DELETE FROM embed_call_log WHERE started_at < ?", (cutoff,))
            # Per-feed FIFO cap via window function (SQLite ≥ 3.25, shipped with
            # Python 3.12). ROW_NUMBER ordered by id DESC: rn=1 is newest, so
            # any row with rn > max_per_feed is older than the kept window.
            # Single index scan vs the prior O(N²) correlated subquery, which
            # could stall the global write lock for seconds at the upper-bound
            # configuration cap.
            await conn.execute(
                "DELETE FROM feed_fetch_log WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY feed_id ORDER BY id DESC"
                "    ) AS rn FROM feed_fetch_log"
                "  ) WHERE rn > ?"
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
        next_run_time=datetime.now(UTC) + timedelta(minutes=5),
        replace_existing=True,
        args=[settings],
    )

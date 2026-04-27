"""APScheduler integration for per-feed collection jobs.

Uses APScheduler 3.11.2 (NOT 4.0 — API is incompatible).
Each feed gets its own IntervalTrigger job so poll_interval_minutes is exact.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.collector.base import BaseSource
from sembr.collector.rss import RSSSource
from sembr.db.sqlite import get_conn
from sembr.models import Feed

logger = logging.getLogger(__name__)

SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    "rss": RSSSource,
}


def register_source(source_type: str, cls: type[BaseSource]) -> None:
    SOURCE_REGISTRY[source_type] = cls


def make_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC")


async def collect_feed(feed_id: int, feed_name: str, feed_url: str, source_type: str, config: dict) -> None:
    source_cls = SOURCE_REGISTRY.get(source_type)
    if source_cls is None:
        logger.error("unknown source_type=%r for feed_id=%d", source_type, feed_id)
        return

    conn = get_conn()

    # Fetch last_collected_at to pass as since, avoiding reprocessing old items
    async with conn.execute("SELECT last_collected_at FROM feeds WHERE id=?", (feed_id,)) as cur:
        row = await cur.fetchone()
    since: datetime | None = None
    if row and row[0]:
        try:
            since = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        except ValueError:
            pass

    timeout = float(config.get("timeout", 30.0))
    source = source_cls(feed_url, timeout=timeout)

    try:
        articles = await source.fetch(since=since)
    except Exception as exc:
        logger.error("fetch failed for feed %r (id=%d): %s", feed_name, feed_id, exc)
        return

    new_count = 0
    for article in articles:
        from sembr.db.feeds import fingerprint_exists, insert_fingerprint  # local import avoids circular at module load
        if not await fingerprint_exists(conn, article.feed_md5):
            await insert_fingerprint(conn, article.feed_md5, feed_id)
            new_count += 1

    from sembr.db.feeds import update_last_collected
    await update_last_collected(conn, feed_id)

    logger.info("fetched %d new items from %r (feed_id=%d, total_seen=%d)", new_count, feed_name, feed_id, len(articles))


async def add_feed_job(scheduler: AsyncIOScheduler, feed: Feed) -> None:
    scheduler.add_job(
        collect_feed,
        trigger=IntervalTrigger(minutes=feed.poll_interval_minutes),
        id=f"feed_{feed.id}",
        args=[feed.id, feed.name, str(feed.url), feed.source_type, feed.config],
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # D3: immediate first run
        replace_existing=True,
    )


def remove_feed_job(scheduler: AsyncIOScheduler, feed_id: int) -> None:
    try:
        scheduler.remove_job(f"feed_{feed_id}")
    except Exception:
        pass  # job may not exist if service restarted after delete

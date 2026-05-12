# SPDX-License-Identifier: Apache-2.0
"""APScheduler integration for per-feed collection jobs.

Uses APScheduler 3.11.2 (NOT 4.0 — API is incompatible).
Each feed gets its own IntervalTrigger job so poll_interval_minutes is exact.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.collector.base import BaseSource
from sembr.collector.host_limiter import HostLimiter
from sembr.collector.newsapi import NewsApiSource, newsapi_master_tick
from sembr.collector.phase import derive_jitter_seconds, derive_phase_seconds
from sembr.collector.rss import FetchError, RSSSource
from sembr.config import Settings, get_settings
from sembr.dashboard.events import log_fetch_event
from sembr.db.articles import insert_article_pending
from sembr.db.feeds import fingerprint_exists, insert_fingerprint, update_last_collected
from sembr.db.sqlite import get_conn
from sembr.models import Feed

# Module-level handle for the host limiter so collect_feed (an APScheduler-invoked
# coroutine without access to FastAPI's request/app) can find it without changing
# add_feed_job's signature. set_host_limiter is called from main.lifespan.
_LIMITER_REF: dict[str, HostLimiter | None] = {"limiter": None}


def set_host_limiter(limiter: HostLimiter | None) -> None:
    _LIMITER_REF["limiter"] = limiter


def get_host_limiter() -> HostLimiter | None:
    """Return the process-wide host rate limiter, or None if it hasn't been
    installed yet. Used by callers outside the APScheduler tick path
    (e.g. /feeds/{id}/fire?dry_run=true) so they can reuse the same
    per-host concurrency budget."""
    return _LIMITER_REF.get("limiter")


logger = logging.getLogger(__name__)

SOURCE_REGISTRY: dict[str, type[BaseSource]] = {
    "rss": RSSSource,
    "newsapi": NewsApiSource,
}

NEWSAPI_MASTER_JOB_ID = "source_newsapi_master"


def register_source(source_type: str, cls: type[BaseSource]) -> None:
    SOURCE_REGISTRY[source_type] = cls


def make_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler(timezone="UTC")


async def _emit_fetch_event(
    *,
    feed_id: int,
    started_at: datetime,
    ok: bool,
    items_seen: int,
    items_new: int,
    error_class: str | None,
    error_message: str | None,
) -> None:
    """Best-effort wrapper: observability faults must never poison collect_feed."""
    try:
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        await log_fetch_event(
            feed_id=feed_id,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            ok=ok,
            items_seen=items_seen,
            items_new=items_new,
            error_class=error_class,
            error_message=error_message,
        )
    except Exception as exc:
        logger.warning("log_fetch_event failed for feed_id=%d: %s", feed_id, exc)


async def collect_feed(
    feed_id: int, feed_name: str, feed_url: str, source_type: str, config: dict
) -> tuple[int, int, list[dict]]:
    """Run one collection pass. Returns (items_seen, items_new, articles).

    `articles` is one dict per fetched article with title/url/published_at/status
    ("NEW" or "DUP") so feed fire popups can render the same shape as dry run.
    Returns (0, 0, []) on configuration errors or fetch failures.

    source_type='newsapi' only reaches this function via feeds_fire — master
    tick owns scheduled polling (add_feed_job:200). Fire forces since=None
    and skips update_last_collected so the already-paid 1 token returns a
    full page of 100, while the master tick's watermark math stays intact.
    """
    source_cls = SOURCE_REGISTRY.get(source_type)
    if source_cls is None:
        # Configuration error, not a fetch attempt — don't write an event row.
        logger.error("unknown source_type=%r for feed_id=%d", source_type, feed_id)
        return 0, 0, []

    conn = get_conn()

    since: datetime | None = None
    if source_type != "newsapi":
        async with conn.execute(
            "SELECT last_collected_at FROM feeds WHERE id=?", (feed_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row[0]:
            try:
                since = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            except ValueError:
                pass

    timeout = float(config.get("timeout", 30.0))
    source = source_cls(feed_url, timeout=timeout)

    # Cap concurrent fetches to the same group_key. Limiter is initialised in
    # main.lifespan; if absent (e.g. unit test that calls collect_feed directly),
    # skip the gate so tests don't need to wire app.state.
    limiter: HostLimiter | None = _LIMITER_REF.get("limiter")
    fetch_ctx = (
        limiter.acquire(limiter.group_key_for(feed_url)) if limiter is not None else nullcontext()
    )
    # Two timestamps so feed_fetch_log.elapsed_ms reflects ACTUAL fetch time, not
    # queue-wait time; SC#5 / SC#6 dashboard evidence depends on this distinction.
    # queued_at: scheduler triggered → we entered the limiter ctx
    # started_at: limiter acquired → we are about to call source.fetch
    # On exception inside acquire (rare), started_at falls back to queued_at.
    # (Loop 2 review #🟡-2)
    queued_at = datetime.now(timezone.utc)
    started_at = queued_at
    try:
        async with fetch_ctx:
            started_at = datetime.now(timezone.utc)
            articles = await source.fetch(since=since)
    except FetchError as exc:
        # Don't advance last_collected_at on failure — next run will retry the
        # same since window so articles published during the outage aren't lost.
        logger.error("fetch failed for feed %r (id=%d): %s", feed_name, feed_id, exc)
        await _emit_fetch_event(
            feed_id=feed_id,
            started_at=started_at,
            ok=False,
            items_seen=0,
            items_new=0,
            error_class="FetchError",
            error_message=str(exc),
        )
        return 0, 0, []
    except Exception as exc:
        logger.error(
            "unexpected error in collect_feed for %r (id=%d): %s",
            feed_name,
            feed_id,
            exc,
            exc_info=True,
        )
        await _emit_fetch_event(
            feed_id=feed_id,
            started_at=started_at,
            ok=False,
            items_seen=0,
            items_new=0,
            error_class=exc.__class__.__name__,
            error_message=str(exc),
        )
        return 0, 0, []

    # Fetch succeeded (articles may be empty if the source has no new content).
    # Always advance the cursor so we don't re-scan the same window next run.
    new_count = 0
    article_results: list[dict] = []
    for article in articles:
        is_new = False
        try:
            is_new = await insert_article_pending(conn, article, feed_id)
            if is_new:
                new_count += 1
        except Exception as exc:
            # One bad article must not abort the rest of the feed's batch.
            logger.error(
                "failed to buffer article %r (feed_id=%d): %s",
                article.url,
                feed_id,
                exc,
                exc_info=True,
            )
        article_results.append(
            {
                "title": article.title,
                "url": article.url,
                "published_at": article.published_at.isoformat() if article.published_at else None,
                "status": "NEW" if is_new else "DUP",
            }
        )

    if source_type != "newsapi":
        await update_last_collected(conn, feed_id)

    logger.info(
        "fetched %d new items from %r (feed_id=%d, total_seen=%d)",
        new_count,
        feed_name,
        feed_id,
        len(articles),
    )
    await _emit_fetch_event(
        feed_id=feed_id,
        started_at=started_at,
        ok=True,
        items_seen=len(articles),
        items_new=new_count,
        error_class=None,
        error_message=None,
    )
    return len(articles), new_count, article_results


async def add_feed_job(scheduler: AsyncIOScheduler, feed: Feed) -> None:
    # newsapi feeds collapse onto a singleton master job — register that
    # instead of a per-feed IntervalTrigger so all enabled newsapi feeds share
    # one tick (1 token / cycle).
    if feed.source_type == "newsapi":
        await ensure_newsapi_master_job(scheduler, get_settings())
        return

    period_s = feed.poll_interval_minutes * 60
    phase_s = derive_phase_seconds(feed.id, period_s)
    jitter_s = derive_jitter_seconds(period_s)
    # Hash-based phase makes first-run distribution deterministic across restarts.
    # Per-fire jitter on top of IntervalTrigger keeps the time series desynchronised.
    scheduler.add_job(
        collect_feed,
        trigger=IntervalTrigger(minutes=feed.poll_interval_minutes, jitter=jitter_s),
        id=f"feed_{feed.id}",
        args=[feed.id, feed.name, str(feed.url), feed.source_type, feed.config],
        coalesce=True,
        max_instances=1,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=phase_s),
        replace_existing=True,
    )


async def remove_feed_job(scheduler: AsyncIOScheduler, feed_id: int) -> None:
    # Try the per-feed job first (no-op for newsapi feeds since they collapse
    # onto the master job; JobLookupError is the expected path), then call
    # maybe_drop_newsapi_master_job so the last-newsapi-feed deletion tears
    # down the master job.
    try:
        scheduler.remove_job(f"feed_{feed_id}")
    except JobLookupError:
        # Job may not exist if the service restarted after the row was deleted,
        # OR if the feed was source_type='newsapi' and never had its own
        # per-feed job — treat as no-op either way.
        logger.debug("remove_feed_job: feed_%d already absent", feed_id)
    await maybe_drop_newsapi_master_job(scheduler, get_conn())


async def ensure_newsapi_master_job(scheduler: AsyncIOScheduler, settings: Settings) -> None:
    """Register the singleton master job if absent.

    No `next_run_time` argument — explicit `next_run_time=None` is a paused
    state; the trigger computes the first run instead. Jitter spreads ticks
    across runs so multiple sembr instances behind the same NEWSAPI_API_KEY
    don't collide.

    Skips when the job already exists so repeated calls (e.g. multiple newsapi
    feed creations in succession) don't reset `next_run_time` and indefinitely
    delay the next tick. Settings-change re-registration is handled separately
    via the api self-restart path.
    """
    if scheduler.get_job(NEWSAPI_MASTER_JOB_ID) is not None:
        return
    period_s = settings.newsapi_poll_interval_minutes * 60
    jitter_s = derive_jitter_seconds(period_s)
    scheduler.add_job(
        newsapi_master_tick,
        trigger=IntervalTrigger(minutes=settings.newsapi_poll_interval_minutes, jitter=jitter_s),
        id=NEWSAPI_MASTER_JOB_ID,
        coalesce=True,
        max_instances=1,
    )


async def maybe_drop_newsapi_master_job(scheduler: AsyncIOScheduler, conn) -> None:
    """Drop the master job when the last enabled newsapi feed disappears so
    the scheduler doesn't keep firing API calls (and burning tokens) for an
    empty source set."""
    async with conn.execute(
        "SELECT 1 FROM feeds WHERE source_type='newsapi' AND enabled=1 LIMIT 1"
    ) as cur:
        if await cur.fetchone() is not None:
            return
    try:
        scheduler.remove_job(NEWSAPI_MASTER_JOB_ID)
    except JobLookupError:
        # Already absent (e.g. master never registered because the feed was
        # added while disabled) — no-op.
        return

"""POST /feeds/{id}/fire + GET /feeds/{id}/fire/{task_id} (D7, D10–D13).

Fire triggers an immediate feed collection or a dry-run fingerprint check.
Results are stored in memory (FeedFireTask) and polled via GET.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status

from sembr.collector.fire_tasks import (
    FeedFireTask,
    create_task,
    get_task,
    throttle_check,
)
from sembr.collector.rss import FetchError
from sembr.collector.scheduler import SOURCE_REGISTRY, _LIMITER_REF, _nullcontext, collect_feed
from sembr.db.feeds import fingerprint_exists, get_feed
from sembr.db.sqlite import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feeds", tags=["feeds"])

# Strong references to in-flight background tasks (GC guard — D10).
_BG_TASKS: set[asyncio.Task] = set()


async def _feed_dry_run(task: FeedFireTask, feed_url: str, source_type: str, config: dict, since: datetime | None) -> None:
    """Background: fetch articles and classify NEW/DUP without writing to DB.

    Reuses _LIMITER_REF for host-rate-limiting (D12) — same path as collect_feed.
    Does NOT write to feed_items, feed_fetch_log, or pending_articles.
    """
    conn = get_conn()
    source_cls = SOURCE_REGISTRY.get(source_type)
    if source_cls is None:
        task.status = "error"
        task.error = f"unknown source_type: {source_type!r}"
        task.finished_at = datetime.now(timezone.utc)
        return

    timeout = float(config.get("timeout", 30.0))
    source = source_cls(feed_url, timeout=timeout)

    limiter = _LIMITER_REF.get("limiter")
    fetch_ctx = (
        limiter.acquire(limiter.group_key_for(feed_url))
        if limiter is not None
        else _nullcontext()
    )

    try:
        async with fetch_ctx:
            articles = await source.fetch(since=since)
    except FetchError as exc:
        task.status = "error"
        task.error = str(exc)
        task.finished_at = datetime.now(timezone.utc)
        return
    except Exception as exc:
        logger.exception("dry_run fetch failed for feed_url=%r: %s", feed_url, exc)
        task.status = "error"
        task.error = str(exc)
        task.finished_at = datetime.now(timezone.utc)
        return

    result_articles = []
    new_count = 0
    for article in articles:
        is_dup = await fingerprint_exists(conn, article.feed_md5)
        status_label = "DUP" if is_dup else "NEW"
        if not is_dup:
            new_count += 1
        result_articles.append({
            "title": article.title,
            "url": article.url,
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "status": status_label,
        })

    task.articles_fetched = len(articles)
    task.articles_new = new_count
    task.articles = result_articles
    task.status = "done"
    task.finished_at = datetime.now(timezone.utc)


async def _feed_real_run(task: FeedFireTask, feed_id: int, feed_name: str, feed_url: str, source_type: str, config: dict) -> None:
    """Background: run collect_feed (writes feed_items, pending_articles, feed_fetch_log)."""
    try:
        await collect_feed(feed_id, feed_name, feed_url, source_type, config)
        task.status = "done"
    except Exception as exc:
        logger.exception("feed fire real_run failed for feed_id=%d: %s", feed_id, exc)
        task.status = "error"
        task.error = str(exc)
    finally:
        task.finished_at = datetime.now(timezone.utc)


@router.post("/{feed_id}/fire", status_code=status.HTTP_202_ACCEPTED)
async def post_feed_fire(
    feed_id: int,
    dry_run: bool = Query(default=False),
) -> dict[str, Any]:
    conn = get_conn()
    feed = await get_feed(conn, feed_id)
    if feed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    # D8: rate limit only applies to real runs
    if not dry_run and not throttle_check(feed_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="fire rate limit: 1 fire per feed per 60 seconds",
        )

    task = create_task(feed_id, dry_run=dry_run)

    if dry_run:
        # Dry run ignores last_collected_at so the user always sees the full
        # current feed contents, not a window relative to the last real fetch.
        bg = asyncio.create_task(
            _feed_dry_run(task, str(feed.url), feed.source_type, feed.config, None)
        )
    else:
        # D10: disabled feeds can also be fired (B2) — use create_task directly, not scheduler
        bg = asyncio.create_task(
            _feed_real_run(task, feed_id, feed.name, str(feed.url), feed.source_type, feed.config)
        )

    _BG_TASKS.add(bg)
    bg.add_done_callback(_BG_TASKS.discard)

    return {
        "task_id": task.task_id,
        "status_url": f"/feeds/{feed_id}/fire/{task.task_id}",
    }


@router.get("/{feed_id}/fire/{task_id}")
async def get_feed_fire_status(feed_id: int, task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task is None or task.feed_id != feed_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    return {
        "task_id": task.task_id,
        "feed_id": task.feed_id,
        "dry_run": task.dry_run,
        "status": task.status,
        "started_at": task.started_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "articles_fetched": task.articles_fetched,
        "articles_new": task.articles_new,
        "articles": task.articles,
        "error": task.error,
    }

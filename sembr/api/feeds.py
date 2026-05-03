"""POST/GET/DELETE /feeds router."""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response, status

from sembr.collector.scheduler import add_feed_job, remove_feed_job
from sembr.db.feed_tags import get_tags, replace_tags_in_tx
from sembr.db.feeds import create_feed, delete_feed, get_feed, list_feeds
from sembr.db.intents import get_intent, intents_remove_feed_id
from sembr.db.sqlite import get_conn, transaction
from sembr.matcher.jobs import reregister_intent_job
from sembr.models import Feed, FeedCreate, FeedTagsUpdate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/feeds", tags=["feeds"])


@router.post("", response_model=Feed, status_code=status.HTTP_201_CREATED)
async def post_feed(body: FeedCreate, request: Request) -> Feed:
    conn = get_conn()
    try:
        feed = await create_feed(
            conn,
            name=body.name,
            url=body.url,
            source_type=body.source_type,
            config=body.config,
            poll_interval_minutes=body.poll_interval_minutes,
            tags=body.tags,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="feed URL already exists") from exc

    scheduler = request.app.state.scheduler
    try:
        await add_feed_job(scheduler, feed)
    except Exception as exc:
        try:
            await delete_feed(conn, feed.id)
        except Exception as rollback_exc:
            logger.error("rollback failed for feed_id=%d: %s", feed.id, rollback_exc)
        try:
            scheduler.remove_job(f"feed_{feed.id}")
        except Exception:
            pass
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to schedule feed") from exc

    return feed


@router.get("", response_model=list[Feed])
async def get_feeds() -> list[Feed]:
    return await list_feeds(get_conn())


@router.patch("/{feed_id}/tags", response_model=Feed)
async def patch_feed_tags(feed_id: int, body: FeedTagsUpdate) -> Feed:
    conn = get_conn()
    async with conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    async with transaction() as txn:
        await replace_tags_in_tx(txn, feed_id, body.tags)
    feed = await get_feed(conn, feed_id)
    if feed is None:  # racing DELETE — surface as 404 instead of 500
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    return feed


@router.delete("/{feed_id}")
async def remove_feed(feed_id: int, request: Request) -> Response:
    conn = get_conn()

    # Existence check before any write (avoids committing a cascade for a non-existent feed)
    async with conn.execute("SELECT id FROM feeds WHERE id=?", (feed_id,)) as cur:
        if not await cur.fetchone():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")

    # DD3: cascade — remove feed_id from all intent feed_filter.ids (no commit yet)
    cascaded_intents = await intents_remove_feed_id(conn, feed_id)
    # delete_feed issues conn.commit(), committing both the cascade UPDATE and the DELETE atomically
    await delete_feed(conn, feed_id)

    remove_feed_job(request.app.state.scheduler, feed_id)

    # Reregister affected intent jobs so updated scan filter takes effect immediately
    scheduler = request.app.state.scheduler
    for iid in cascaded_intents:
        intent = await get_intent(conn, iid)
        if intent is not None and intent.enabled:
            try:
                reregister_intent_job(scheduler, intent, request.app)
            except Exception as exc:
                logger.warning(
                    "feed delete: reregister intent_id=%d failed: %s (recovers on restart)",
                    iid,
                    exc,
                )

    return Response(status_code=status.HTTP_204_NO_CONTENT)

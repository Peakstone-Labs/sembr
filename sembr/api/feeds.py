"""POST/GET/DELETE /feeds router."""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response, status

from sembr.collector.scheduler import add_feed_job, remove_feed_job
from sembr.db.feeds import create_feed, delete_feed, list_feeds
from sembr.db.sqlite import get_conn
from sembr.models import Feed, FeedCreate

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


@router.delete("/{feed_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def remove_feed(feed_id: int, request: Request) -> None:
    conn = get_conn()
    existed = await delete_feed(conn, feed_id)
    if not existed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="feed not found")
    remove_feed_job(request.app.state.scheduler, feed_id)

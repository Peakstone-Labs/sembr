"""POST /intents/{id}/fire + GET /intents/{id}/fire/{task_id} (DD8).

Fire triggers an immediate intent scan outside the APScheduler tick.
Results are stored in memory (FireTask) and can be polled via GET.
The fire path never writes to match_seen (write_match_seen=False).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.matcher.fire_tasks import FireTask, create_task, get_task, throttle_check
from sembr.matcher.scan import ScanOptions, scan_once
from sembr.models import CronSchedule

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intents"])

# Strong references to in-flight background tasks (asyncio GC guard).
# Entries are discarded via add_done_callback when the coroutine finishes.
_BG_TASKS: set[asyncio.Task] = set()


async def _fire_run(
    task: FireTask,
    options: ScanOptions,
    app,
) -> None:
    """Background task: scan → update task state → push if matches found."""
    try:
        conn = get_conn()
        intent = await get_intent(conn, task.intent_id)
        if intent is None:
            task.status = "error"
            logger.error("fire task_id=%s: intent_id=%d not found", task.task_id, task.intent_id)
            return

        qdrant_client = app.state.qdrant.client

        matches = await scan_once(intent, options, conn, qdrant_client)

        task.match_count = len(matches)
        task.matches = [
            {
                "article_id": m.article_id,
                "score": m.score,
                "title": (m.payload or {}).get("title", ""),
                "url": (m.payload or {}).get("url", ""),
                "published_at": (m.payload or {}).get("published_at"),
            }
            for m in matches
        ]

        if matches:
            on_match = app.state.on_match
            if on_match is not None:
                await on_match(matches)
                task.pushed = True
        # TODO(intent-control follow-up): notify_on_empty hook

        task.status = "done"
    except Exception:
        logger.exception(
            "fire task_id=%s intent_id=%d unexpected error",
            task.task_id,
            task.intent_id,
        )
        task.status = "error"
    finally:
        task.finished_at = datetime.now(timezone.utc)


@router.post(
    "/intents/{intent_id}/fire",
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_fire(
    intent_id: int,
    request: Request,
    lookback: int | None = Query(default=None, ge=300, le=2592000),
    skip_seen: bool | None = Query(default=None),
    threshold: float | None = Query(default=None, ge=0.20, le=0.95),
) -> dict[str, Any]:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    if not isinstance(intent.schedule, CronSchedule):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="POST /fire is only valid for cron-mode intents; event-mode intents trigger via ingestion.",
        )

    if not throttle_check(intent_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="fire rate limit: 1 request per intent per 60s",
        )

    options = ScanOptions(
        lookback_seconds=lookback if lookback is not None else intent.schedule.lookback_seconds,
        threshold=threshold if threshold is not None else intent.threshold,
        skip_seen=skip_seen if skip_seen is not None else intent.schedule.skip_seen,
        feed_ids=intent.feed_filter.ids if intent.feed_filter else None,
        write_match_seen=False,  # fire never writes match_seen
    )

    task = create_task(intent_id)

    bg = asyncio.create_task(_fire_run(task, options, request.app))
    _BG_TASKS.add(bg)
    bg.add_done_callback(_BG_TASKS.discard)

    return {
        "task_id": task.task_id,
        "status_url": f"/intents/{intent_id}/fire/{task.task_id}",
    }


@router.get("/intents/{intent_id}/fire/{task_id}")
async def get_fire_status(intent_id: int, task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task is None or task.intent_id != intent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")

    return {
        "task_id": task.task_id,
        "intent_id": task.intent_id,
        "status": task.status,
        "started_at": task.started_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "match_count": task.match_count,
        "matches": task.matches,
        "pushed": task.pushed,
        "push_error": task.push_error,
    }

"""POST/GET endpoints for the maintenance Dashboard panel.

Endpoints (prefix `/api/dashboard/maintenance`, gated by
``DashboardTokenMiddleware``):

- ``GET  /feed_universe``                       feed picker data
- ``POST /manual_prune``                        create planning task
- ``GET  /manual_prune/{task_id}``              poll task state
- ``POST /manual_prune/{task_id}/confirm``      transition planned → applying

The planning + applying paths run in background tasks held by ``_BG_TASKS``
so a slow Qdrant doesn't block the HTTP request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from sembr.db.sqlite import get_conn
from sembr.maintenance import manual_prune, tasks as mp_tasks
from sembr.vector_store.news import ALIAS_NAME

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dashboard/maintenance", tags=["maintenance"])

# Strong references to in-flight planning / applying coroutines so the GC
# can't drop them mid-flight (mirrors sembr/api/feeds_fire.py:_BG_TASKS).
_BG_TASKS: set[asyncio.Task] = set()


class ManualPruneRequest(BaseModel):
    target: Literal["news", "dead"]
    feed_ids: list[int] = Field(min_length=1)
    older_than_days: int = Field(ge=1, le=3650)


def _serialise_task(task: mp_tasks.ManualPruneTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "target": task.target,
        "feed_ids": task.feed_ids,
        "older_than_days": task.older_than_days,
        "status": task.status,
        "started_at": task.started_at.isoformat(),
        "finished_at": (task.finished_at.isoformat() if task.finished_at else None),
        "plan_summary": task.plan_summary,
        "result_summary": task.result_summary,
        "error": task.error,
    }


def _spawn(coro) -> None:
    bg = asyncio.create_task(coro)
    _BG_TASKS.add(bg)
    bg.add_done_callback(_BG_TASKS.discard)


@router.get("/feed_universe")
async def get_feed_universe(request: Request) -> dict[str, Any]:
    """Return ``{"alive": [...], "deleted": [...]}`` for the manual-prune
    picker. Source of truth: Qdrant ``feed_id`` facet ∪ SQLite ``feeds``.

    Implementation strategy: ``client.facet(... key="feed_id", limit=200,
    exact=False)``. ``exact=False`` is fine for picker listings — only the
    dry-run path needs exact counts.
    """
    qdrant = getattr(request.app.state, "qdrant", None)
    if qdrant is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant not initialised",
        )
    try:
        res = await qdrant.client.facet(
            collection_name=ALIAS_NAME,
            key="feed_id",
            limit=200,
            exact=False,
        )
    except Exception as exc:
        logger.exception("feed_universe: facet call failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Qdrant facet failed: {exc}",
        )
    qdrant_feed_ids = {int(h.value) for h in res.hits}

    conn = get_conn()
    async with conn.execute("SELECT id, name FROM feeds") as cur:
        sqlite_feeds = {int(r[0]): r[1] for r in await cur.fetchall()}

    alive = [
        {"id": fid, "name": sqlite_feeds[fid]}
        for fid in sorted(qdrant_feed_ids)
        if fid in sqlite_feeds
    ]
    deleted = [
        {"id": fid, "name": None} for fid in sorted(qdrant_feed_ids) if fid not in sqlite_feeds
    ]
    return {"alive": alive, "deleted": deleted}


@router.post("/manual_prune", status_code=status.HTTP_202_ACCEPTED)
async def post_manual_prune(request: Request, body: ManualPruneRequest) -> dict[str, Any]:
    qdrant = getattr(request.app.state, "qdrant", None)
    if body.target == "news" and qdrant is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant not initialised",
        )
    task = mp_tasks.create_task(
        target=body.target,
        feed_ids=body.feed_ids,
        older_than_days=body.older_than_days,
    )
    _spawn(manual_prune.run_planning(task, qdrant))
    return {
        "task_id": task.task_id,
        "status_url": f"/api/dashboard/maintenance/manual_prune/{task.task_id}",
    }


@router.get("/manual_prune/{task_id}")
async def get_manual_prune_status(task_id: str) -> dict[str, Any]:
    task = mp_tasks.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _serialise_task(task)


@router.post(
    "/manual_prune/{task_id}/confirm",
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_manual_prune_confirm(request: Request, task_id: str) -> dict[str, Any]:
    task = mp_tasks.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if task.status != "planned":
        # 409 Conflict is the right code: the resource exists but is in a
        # state that does not permit confirm. The client should re-poll or
        # re-create the task.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task status is {task.status!r}, expected 'planned'",
        )

    qdrant = getattr(request.app.state, "qdrant", None)
    if task.target == "news" and qdrant is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant not initialised",
        )

    task.status = "applying"
    _spawn(manual_prune.run_applying(task, qdrant))
    return {"task_id": task.task_id, "status": task.status}

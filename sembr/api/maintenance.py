# SPDX-License-Identifier: Apache-2.0
"""POST/GET endpoints for the maintenance Dashboard panel.

Endpoints (prefix `/api/dashboard/maintenance`, gated by
``DashboardTokenMiddleware``):

- ``GET  /qdrant_stats``                        per-segment vector-store health
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
from sembr.maintenance import manual_prune
from sembr.maintenance import tasks as mp_tasks
from sembr.maintenance.derived_backfill import count_pending_derived
from sembr.vector_store.news import ALIAS_NAME, collection_name
from sembr.vector_store.news_archive import ARCHIVE_ALIAS, archive_collection_name

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
        ) from exc
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


# ---------------------------------------------------------------------------
# Vector-store health
#
# `/api/news/search` hides the two-collection split from callers on purpose.
# Operations is the one audience that must see it: the container's memory
# ceiling is shared, so "RSS is climbing" is only actionable once you know
# WHICH store is growing. Hence per-segment counts here and nowhere else.
# ---------------------------------------------------------------------------

_STATS_SEGMENTS = ("current", "archive")


async def _segment_stats(client: Any, alias: str, expected_collection: str) -> dict[str, Any]:
    async def _edge_ts(direction: str) -> int | None:
        points, _ = await client.scroll(
            collection_name=alias,
            with_payload=True,
            with_vectors=False,
            order_by={"key": "ingested_at_ts", "direction": direction},
            limit=1,
        )
        if not points:
            return None
        ts = (points[0].payload or {}).get("ingested_at_ts")
        return ts if isinstance(ts, int) else None

    async def _alias_ok() -> bool | None:
        # Health bit for the silent-degradation window when an alias points at
        # a different model generation than the live embedder (semantic scores
        # unreliable / migrations may fail on dimension mismatch). Boolean
        # only — physical collection names stay out of the contract.
        try:
            aliases = await client.get_aliases()
            target = {a.alias_name: a.collection_name for a in aliases.aliases}.get(alias)
            if target is None:
                return False
            return target == expected_collection
        except Exception:
            logger.warning("qdrant_stats: alias check failed for %r", alias, exc_info=True)
            return None

    count_result = await client.count(collection_name=alias, exact=True)
    return {
        "points_count": count_result.count,
        "earliest_ingested_at_ts": await _edge_ts("asc"),
        "latest_ingested_at_ts": await _edge_ts("desc"),
        "alias_ok": await _alias_ok(),
    }


@router.get("/qdrant_stats")
async def get_qdrant_stats(request: Request) -> dict[str, Any]:
    """Point counts, ingestion time ranges and alias health, per segment.

    Independent of the retention job's own log counters, so migration
    conservation can be reconciled from two sides. ``derived_backfill_pending``
    is recomputed from Qdrant on every call rather than read back from the
    backfill job's published flag — "the job says it finished" and "no point is
    missing the field" are different claims, and only the second one is the
    acceptance criterion.
    """
    qdrant = getattr(request.app.state, "qdrant", None)
    if qdrant is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant not initialised",
        )
    client = qdrant.client
    model_version = request.app.state.embedder.model_version
    expected = {
        "current": (ALIAS_NAME, collection_name(model_version)),
        "archive": (ARCHIVE_ALIAS, archive_collection_name(model_version)),
    }

    segments: dict[str, Any] = {}
    try:
        for name in _STATS_SEGMENTS:
            alias, expected_collection = expected[name]
            segments[name] = await _segment_stats(client, alias, expected_collection)
        segments["current"]["derived_backfill_pending"] = await count_pending_derived(client)
        # Split out of the pending count rather than deducted from it: a
        # quarantined point genuinely lacks its derived fields, so removing it
        # would turn "no point is missing the field" into the weaker "the job
        # thinks it is done". Reported separately so a stuck backfill is
        # distinguishable from one still draining. Process-local — a restart
        # resets it to 0 and the next three rounds rediscover it. `null` (not
        # 0) when the handle is absent, so "no state" never reads as "clean".
        backfill_state = getattr(request.app.state, "news_derived_backfill_state", None)
        segments["current"]["derived_backfill_quarantined"] = (
            len(backfill_state.quarantined) if backfill_state is not None else None
        )
    except Exception as exc:
        logger.exception("qdrant_stats: query failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Qdrant stats failed: {str(exc)[:200]}",
        ) from exc

    return {"segments": segments}

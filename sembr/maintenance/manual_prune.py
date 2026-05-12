# SPDX-License-Identifier: Apache-2.0
"""Manual prune planning + apply (state machine implementation).

Two background coroutines per task:

- ``run_planning(task, qdrant_handle)`` → reads-only dry-run, transitions to
  ``planned`` (with ``plan_summary``).
- ``run_applying(task, qdrant_handle)`` → real delete + cascade, transitions
  to ``done`` (with ``result_summary``).

Both write all error paths to ``task.status = "error"`` + ``task.error`` so
the status endpoint can surface them.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING

from sembr.db.sqlite import get_conn, transaction
from sembr.maintenance.qdrant_ttl import _cascade_delete_sqlite
from sembr.maintenance.tasks import ManualPruneTask
from sembr.vector_store.news import ALIAS_NAME

if TYPE_CHECKING:
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

_QDRANT_DELETE_BATCH = 1000
_SCROLL_BATCH = 1000


def _news_cutoff_ts(older_than_days: int) -> int:
    return int(time.time()) - older_than_days * 86400


def _dead_cutoff_iso(older_than_days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()


async def _facet_news_counts(
    qdrant_handle: "QdrantHandle",
    feed_ids: list[int],
    cutoff_ts: int,
) -> dict[int, int]:
    """Return ``{feed_id: would_delete}`` for the News dry-run.

    Uses ``client.facet`` with ``exact=True`` because the user sees this
    number on the Confirm screen — an estimate would invite confusion.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchAny,
        Range,
    )

    if not feed_ids:
        return {}
    facet_filter = Filter(
        must=[
            FieldCondition(key="ingested_at_ts", range=Range(lt=cutoff_ts)),
            FieldCondition(key="feed_id", match=MatchAny(any=feed_ids)),
        ]
    )
    res = await qdrant_handle.client.facet(
        collection_name=ALIAS_NAME,
        key="feed_id",
        facet_filter=facet_filter,
        limit=max(len(feed_ids), 1),
        exact=True,
    )
    out: dict[int, int] = {}
    for hit in res.hits:
        try:
            out[int(hit.value)] = int(hit.count)
        except (TypeError, ValueError):
            continue
    return out


async def _scroll_news_uuids(
    qdrant_handle: "QdrantHandle",
    feed_ids: list[int],
    cutoff_ts: int,
) -> list[str]:
    """Collect the Qdrant point ids that match the prune filter, paged through
    scroll. Used only by ``run_applying`` (real delete path).
    """
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        MatchAny,
        Range,
    )

    if not feed_ids:
        return []
    qfilter = Filter(
        must=[
            FieldCondition(key="ingested_at_ts", range=Range(lt=cutoff_ts)),
            FieldCondition(key="feed_id", match=MatchAny(any=feed_ids)),
        ]
    )
    out: list[str] = []
    next_offset = None
    while True:
        points, next_offset = await qdrant_handle.client.scroll(
            collection_name=ALIAS_NAME,
            scroll_filter=qfilter,
            limit=_SCROLL_BATCH,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        out.extend(str(p.id) for p in points)
        if next_offset is None:
            break
    return out


async def _delete_news_points(qdrant_handle: "QdrantHandle", uuids: list[str]) -> None:
    from qdrant_client.models import PointIdsList  # noqa: PLC0415

    for i in range(0, len(uuids), _QDRANT_DELETE_BATCH):
        chunk = uuids[i : i + _QDRANT_DELETE_BATCH]
        await qdrant_handle.client.delete(
            collection_name=ALIAS_NAME,
            points_selector=PointIdsList(points=chunk),
        )


async def _dead_counts_by_feed(feed_ids: list[int], cutoff_iso: str) -> dict[int, int]:
    if not feed_ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(feed_ids))
    sql = (
        f"SELECT feed_id, COUNT(*) FROM dead_articles "
        f"WHERE failed_at < ? AND feed_id IN ({placeholders}) "
        f"GROUP BY feed_id"
    )
    params = [cutoff_iso, *feed_ids]
    async with conn.execute(sql, params) as cur:
        rows = await cur.fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


async def _resolve_feed_names(feed_ids: list[int]) -> dict[int, str | None]:
    """Return ``{feed_id: name or None}``; deleted-feed ids resolve to None."""
    if not feed_ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(feed_ids))
    async with conn.execute(
        f"SELECT id, name FROM feeds WHERE id IN ({placeholders})", feed_ids
    ) as cur:
        rows = await cur.fetchall()
    found = {int(r[0]): r[1] for r in rows}
    return {fid: found.get(fid) for fid in feed_ids}


async def run_planning(task: ManualPruneTask, qdrant_handle: "QdrantHandle | None") -> None:
    """Compute the dry-run plan_summary and transition the task to ``planned``."""
    try:
        feed_names = await _resolve_feed_names(task.feed_ids)
        if task.target == "news":
            if qdrant_handle is None:
                raise RuntimeError("News dry-run requires a Qdrant handle")
            cutoff_ts = _news_cutoff_ts(task.older_than_days)
            counts = await _facet_news_counts(
                qdrant_handle,
                task.feed_ids,
                cutoff_ts,
            )
        else:
            cutoff_iso = _dead_cutoff_iso(task.older_than_days)
            counts = await _dead_counts_by_feed(task.feed_ids, cutoff_iso)

        feeds_summary = []
        total = 0
        for fid in task.feed_ids:
            name = feed_names.get(fid)
            n = int(counts.get(fid, 0))
            feeds_summary.append(
                {
                    "feed_id": fid,
                    "feed_name": name,
                    "deleted": name is None,
                    "would_delete": n,
                }
            )
            total += n
        task.plan_summary = {
            "target": task.target,
            "older_than_days": task.older_than_days,
            "feeds": feeds_summary,
            "total_would_delete": total,
        }
        task.status = "planned"
    except Exception as exc:
        logger.exception("manual_prune planning failed for task_id=%s", task.task_id)
        task.status = "error"
        task.error = str(exc)
        task.finished_at = datetime.now(timezone.utc)


async def _apply_news(task: ManualPruneTask, qdrant_handle: "QdrantHandle") -> dict:
    started_at = monotonic()
    cutoff_ts = _news_cutoff_ts(task.older_than_days)
    purge_uuids = await _scroll_news_uuids(
        qdrant_handle,
        task.feed_ids,
        cutoff_ts,
    )
    qdrant_deleted = len(purge_uuids)
    if purge_uuids:
        await _delete_news_points(qdrant_handle, purge_uuids)
        deleted_fi, deleted_ms = await _cascade_delete_sqlite(purge_uuids)
    else:
        deleted_fi = deleted_ms = 0
    elapsed_ms = int((monotonic() - started_at) * 1000)
    return {
        "target": "news",
        "deleted_qdrant": qdrant_deleted,
        "deleted_feed_items": deleted_fi,
        "deleted_match_seen": deleted_ms,
        "elapsed_ms": elapsed_ms,
    }


async def _apply_dead(task: ManualPruneTask) -> dict:
    started_at = monotonic()
    cutoff_iso = _dead_cutoff_iso(task.older_than_days)
    if not task.feed_ids:
        return {
            "target": "dead",
            "deleted_dead_articles": 0,
            "elapsed_ms": int((monotonic() - started_at) * 1000),
        }
    placeholders = ",".join("?" * len(task.feed_ids))
    sql = f"DELETE FROM dead_articles WHERE failed_at < ? AND feed_id IN ({placeholders})"
    params = [cutoff_iso, *task.feed_ids]
    deleted = 0
    async with transaction() as txn:
        await txn.execute(sql, params)
        async with txn.execute("SELECT changes()") as cur:
            deleted = (await cur.fetchone())[0]
    elapsed_ms = int((monotonic() - started_at) * 1000)
    return {
        "target": "dead",
        "deleted_dead_articles": deleted,
        "elapsed_ms": elapsed_ms,
    }


async def run_applying(task: ManualPruneTask, qdrant_handle: "QdrantHandle | None") -> None:
    """Execute the real delete and transition the task to ``done``."""
    try:
        if task.target == "news":
            if qdrant_handle is None:
                raise RuntimeError("News apply requires a Qdrant handle")
            result = await _apply_news(task, qdrant_handle)
        else:
            result = await _apply_dead(task)

        # Surface a planning↔result drift warning so post-mortems can
        # correlate user-facing surprise with concurrent ingest. 10% threshold
        # is a soft alarm, not a failure (Risk #4).
        plan_total = (task.plan_summary or {}).get("total_would_delete")
        if isinstance(plan_total, int) and plan_total > 0:
            if task.target == "news":
                actual = result.get("deleted_qdrant", 0)
            else:
                actual = result.get("deleted_dead_articles", 0)
            drift = abs(actual - plan_total)
            if drift > 0 and drift / plan_total > 0.10:
                logger.warning(
                    "manual_prune drift > 10%%: task_id=%s target=%s "
                    "plan_total=%d actual=%d "
                    "(status: /api/dashboard/maintenance/manual_prune/%s)",
                    task.task_id,
                    task.target,
                    plan_total,
                    actual,
                    task.task_id,
                )

        task.result_summary = result
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)
    except Exception as exc:
        logger.exception("manual_prune apply failed for task_id=%s", task.task_id)
        task.status = "error"
        task.error = str(exc)
        task.finished_at = datetime.now(timezone.utc)

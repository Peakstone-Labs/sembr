# SPDX-License-Identifier: Apache-2.0
"""Qdrant ``news_current`` TTL job + cascade delete to ``feed_items`` and
``match_seen``.

Order of operations matters: the hard-constraint is "Qdrant delete ≤ SQLite
delete" (so a Qdrant failure can never leave SQLite-orphan ``feed_items``
rows). We scroll first, delete from Qdrant in a batched loop, then cascade
SQLite in chunks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import transaction
from sembr.vector_store.news import ALIAS_NAME, uuid_to_md5

if TYPE_CHECKING:
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

_SCROLL_BATCH = 1000
_QDRANT_DELETE_BATCH = 1000
_SQLITE_DELETE_CHUNK = 500


async def _scroll_expired_uuids(qdrant_handle: "QdrantHandle", cutoff_ts: int) -> list[str]:
    """Scroll ``news_current`` and collect IDs of points with
    ``ingested_at_ts < cutoff_ts``.

    Uses Qdrant's opaque ``offset`` cursor — scroll's ``offset`` is the
    point-id continuation token returned by the previous page, NOT a numeric
    page index.
    """
    # Imported lazily so this module remains importable on dev machines without
    # qdrant_client installed (test convention; mirrors vector_store/news.py).
    from qdrant_client.models import (  # noqa: PLC0415
        FieldCondition,
        Filter,
        Range,
    )

    qfilter = Filter(
        must=[
            FieldCondition(key="ingested_at_ts", range=Range(lt=cutoff_ts)),
        ]
    )

    purge_uuids: list[str] = []
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
        purge_uuids.extend(str(p.id) for p in points)
        if next_offset is None:
            break
    return purge_uuids


async def _delete_qdrant_points(qdrant_handle: "QdrantHandle", uuids: list[str]) -> None:
    from qdrant_client.models import PointIdsList  # noqa: PLC0415

    for i in range(0, len(uuids), _QDRANT_DELETE_BATCH):
        chunk = uuids[i : i + _QDRANT_DELETE_BATCH]
        await qdrant_handle.client.delete(
            collection_name=ALIAS_NAME,
            points_selector=PointIdsList(points=chunk),
        )


async def _cascade_delete_sqlite(uuids: list[str]) -> tuple[int, int]:
    """Delete ``feed_items`` (by md5) and ``match_seen`` (by article_id=uuid)
    rows for every Qdrant id in ``uuids``. Returns (deleted_feed_items,
    deleted_match_seen).

    Each chunk runs in its own short BEGIN..COMMIT — never combine chunks into
    one transaction or the writer queue stalls (Risk #10).
    """
    md5_uuid_pairs: list[tuple[str, str]] = []
    for u in uuids:
        try:
            m = uuid_to_md5(u)
        except ValueError:
            logger.warning("qdrant_ttl: skipping non-uuid point id %r", u)
            continue
        md5_uuid_pairs.append((m, u))

    deleted_fi = 0
    deleted_ms = 0
    for i in range(0, len(md5_uuid_pairs), _SQLITE_DELETE_CHUNK):
        pairs = md5_uuid_pairs[i : i + _SQLITE_DELETE_CHUNK]
        chunk_md5 = [m for m, _ in pairs]
        chunk_uuid = [u for _, u in pairs]
        async with transaction() as txn:
            ph_md5 = ",".join("?" * len(chunk_md5))
            await txn.execute(f"DELETE FROM feed_items WHERE md5 IN ({ph_md5})", chunk_md5)
            # Each DELETE needs its own SELECT changes() — SQLite's changes()
            # only reflects the LAST DML on the connection, so a single read
            # at txn end would silently lose the feed_items count.
            async with txn.execute("SELECT changes()") as cur:
                deleted_fi += (await cur.fetchone())[0]
            ph_uuid = ",".join("?" * len(chunk_uuid))
            await txn.execute(
                f"DELETE FROM match_seen WHERE article_id IN ({ph_uuid})",
                chunk_uuid,
            )
            async with txn.execute("SELECT changes()") as cur:
                deleted_ms += (await cur.fetchone())[0]
        await asyncio.sleep(0)  # defence-in-depth yield
    return deleted_fi, deleted_ms


async def _run_qdrant_ttl(qdrant_handle: "QdrantHandle", settings: Settings) -> None:
    started_at = monotonic()
    cutoff_ts = int(time.time()) - settings.qdrant_news_retention_days * 86400

    try:
        purge_uuids = await _scroll_expired_uuids(qdrant_handle, cutoff_ts)
    except Exception:
        logger.warning("qdrant_ttl: scroll failed", exc_info=True)
        return

    if not purge_uuids:
        elapsed_ms = int((monotonic() - started_at) * 1000)
        logger.info(
            "qdrant_ttl run: cutoff_ts=%d deleted_qdrant=0 deleted_feed_items=0 "
            "deleted_match_seen=0 elapsed_ms=%d interval_hours=%d",
            cutoff_ts,
            elapsed_ms,
            settings.maintenance_interval_hours,
        )
        return

    try:
        await _delete_qdrant_points(qdrant_handle, purge_uuids)
    except Exception:
        logger.warning("qdrant_ttl: delete failed", exc_info=True)
        return

    deleted_fi, deleted_ms = await _cascade_delete_sqlite(purge_uuids)
    elapsed_ms = int((monotonic() - started_at) * 1000)
    logger.info(
        "qdrant_ttl run: cutoff_ts=%d deleted_qdrant=%d deleted_feed_items=%d "
        "deleted_match_seen=%d elapsed_ms=%d interval_hours=%d",
        cutoff_ts,
        len(purge_uuids),
        deleted_fi,
        deleted_ms,
        elapsed_ms,
        settings.maintenance_interval_hours,
    )


def add_qdrant_ttl_job(
    scheduler: AsyncIOScheduler,
    qdrant_handle: "QdrantHandle",
    settings: Settings,
) -> None:
    """Register the Qdrant TTL job with a 15-minute startup offset."""
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        _run_qdrant_ttl,
        trigger=IntervalTrigger(
            hours=settings.maintenance_interval_hours,
            start_date=now + timedelta(minutes=15),
        ),
        id="maintenance_qdrant_ttl",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        args=[qdrant_handle, settings],
    )

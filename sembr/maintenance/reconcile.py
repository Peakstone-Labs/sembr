"""Reconcile job: bulk-delete ``feed_items`` rows whose ``md5`` has no
corresponding Qdrant point in ``news_current`` (Option A: forward full scan).

Runs on the shared maintenance cadence with a 5-minute startup offset (D1).
Strategy and SQL details: design D3.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import get_conn, transaction
from sembr.vector_store.news import ALIAS_NAME, md5_to_uuid

if TYPE_CHECKING:
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

# 1000-id batches stay well under Qdrant's per-request limit and keep network
# RTT amortised (~50ms per batch). Empirical Mac mini measurement: 14k md5 ≈
# 14 batches ≈ 0.7s wall time.
_QDRANT_RETRIEVE_BATCH = 1000

# 500-md5 SQLite chunks stay under the 32766 ?-bind cap with margin and keep
# single-txn wall time < 100ms once the match_seen index (D11) is in place,
# so embedder_worker / collect_feed writers never queue longer than one chunk
# behind reconcile.
_SQLITE_DELETE_CHUNK = 500


async def _run_reconcile(qdrant_handle: "QdrantHandle", settings: Settings) -> None:
    """Scan all ``feed_items.md5`` (excluding in-flight pending) and delete rows
    whose Qdrant point is missing.

    Step 1 excludes ``pending_articles`` to avoid racing with embedder_worker:
    the moment between embedder pulling a row and upserting its point is the
    only orphan-look-alike window, and embedder upsert-then-delete keeps that
    window outside `pending_articles` (Risk row 1).
    """
    started_at = monotonic()
    conn = get_conn()

    async with conn.execute(
        "SELECT fi.md5 FROM feed_items fi "
        "WHERE fi.md5 NOT IN (SELECT md5 FROM pending_articles)"
    ) as cur:
        rows = await cur.fetchall()
    md5_list = [r[0] for r in rows]

    if not md5_list:
        elapsed_ms = int((monotonic() - started_at) * 1000)
        logger.info(
            "reconcile run: scanned=0 found=0 orphan_deleted=0 elapsed_ms=%d",
            elapsed_ms,
        )
        return

    found_ids: set[str] = set()
    for i in range(0, len(md5_list), _QDRANT_RETRIEVE_BATCH):
        batch_md5 = md5_list[i : i + _QDRANT_RETRIEVE_BATCH]
        batch_uuid: list[str] = []
        for m in batch_md5:
            try:
                batch_uuid.append(md5_to_uuid(m))
            except ValueError:
                # `_MD5_RE` already gates inserts (sembr/db/articles.py:95-96)
                # but a corrupted historical row should not poison the whole batch.
                logger.warning("reconcile: skipping non-hex md5 %r", m)
        if not batch_uuid:
            continue
        points = await qdrant_handle.client.retrieve(
            collection_name=ALIAS_NAME,
            ids=batch_uuid,
            with_payload=False,
            with_vectors=False,
        )
        found_ids.update(str(p.id) for p in points)

    orphan_md5: list[str] = []
    for m in md5_list:
        try:
            uid = md5_to_uuid(m)
        except ValueError:
            continue
        if uid not in found_ids:
            orphan_md5.append(m)

    deleted = 0
    for i in range(0, len(orphan_md5), _SQLITE_DELETE_CHUNK):
        chunk = orphan_md5[i : i + _SQLITE_DELETE_CHUNK]
        async with transaction() as txn:
            placeholders = ",".join("?" * len(chunk))
            await txn.execute(
                f"DELETE FROM feed_items WHERE md5 IN ({placeholders})", chunk
            )
            # SELECT changes() must be INSIDE the txn — once COMMIT releases the
            # lock another writer can sneak in before we read the count and
            # we'd see THEIR rowcount (memory: feedback_sqlite_pragmas#3).
            async with txn.execute("SELECT changes()") as cur:
                deleted += (await cur.fetchone())[0]
        # The lock's FIFO ordering (sembr/db/sqlite.py:14-28) already guarantees
        # fairness; the explicit yield is a defence-in-depth no-op (D12).
        await asyncio.sleep(0)

    elapsed_ms = int((monotonic() - started_at) * 1000)
    logger.info(
        "reconcile run: scanned=%d found=%d orphan_deleted=%d elapsed_ms=%d",
        len(md5_list), len(found_ids), deleted, elapsed_ms,
    )


def add_reconcile_job(
    scheduler: AsyncIOScheduler,
    qdrant_handle: "QdrantHandle",
    settings: Settings,
) -> None:
    """Register the reconcile job with a 5-minute startup offset (D1)."""
    now = datetime.now(timezone.utc)
    scheduler.add_job(
        _run_reconcile,
        trigger=IntervalTrigger(
            hours=settings.maintenance_interval_hours,
            start_date=now + timedelta(minutes=5),
        ),
        id="maintenance_reconcile",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        args=[qdrant_handle, settings],
    )

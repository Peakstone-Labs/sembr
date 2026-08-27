# SPDX-License-Identifier: Apache-2.0
"""Qdrant ``news_current`` TTL job: archive expired points, then cascade
delete to ``feed_items`` and ``match_seen``.

Order of operations matters. Per batch the invariant chain is
"archive upsert (acknowledged) ≥ news_current delete ≥ SQLite delete" —
each stage a subset of the previous one — so no failure point can leave an
article in neither store, and a Qdrant failure can never leave SQLite-orphan
``feed_items`` rows. Batches run sequentially and the run aborts on the
first stage failure; whatever was not yet deleted is retried next interval
(upserts are overwrite-idempotent, point ids are stable md5-derived uuids).

``match_seen`` is read for the batch BEFORE its cascade delete — the archived
payload keeps the list of intents that ever matched the article, and those
rows are gone right after.

With ``qdrant_archive_enabled=False`` the job falls back to the pre-archive
pure-delete path, call-for-call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.config import Settings
from sembr.db.sqlite import get_conn, transaction
from sembr.vector_store.news import ALIAS_NAME, uuid_to_md5

if TYPE_CHECKING:
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

_SCROLL_BATCH = 1000
_QDRANT_DELETE_BATCH = 1000
_SQLITE_DELETE_CHUNK = 500
# Full migration batch (retrieve → enrich → upsert → delete → cascade).
# The peak-memory driver is NOT the payload (body p99 ≈ 12.5 KB) but the
# vectors as Python float-object lists: measured 33.4 MB per 1000 × 1024-dim
# retrieve, plus a list() copy (8.3 MB) and the upsert JSON serialization
# (~21 MB) alive at the same time. The api container runs under a 1500m
# mem_limit alongside matcher/summarizer work, so 250 keeps the migration
# peak in the tens-of-MB range regardless of downtime-backlog size.
_ARCHIVE_BATCH = 250


async def _scroll_expired_uuids(qdrant_handle: QdrantHandle, cutoff_ts: int) -> list[str]:
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


async def _delete_qdrant_points(qdrant_handle: QdrantHandle, uuids: list[str]) -> None:
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


async def _fetch_matched_intents(uuids: list[str]) -> dict[str, list[int]]:
    """``match_seen`` intent ids per article uuid for one migration batch.

    Must run BEFORE the batch's cascade delete — the rows are gone right
    after, and the archived payload is the only place this history survives.
    Read-only, chunked to stay under the SQLite bind-parameter cap.
    """
    result: dict[str, list[int]] = {u: [] for u in uuids}
    conn = get_conn()
    for i in range(0, len(uuids), _SQLITE_DELETE_CHUNK):
        chunk = uuids[i : i + _SQLITE_DELETE_CHUNK]
        ph = ",".join("?" * len(chunk))
        async with conn.execute(
            f"SELECT article_id, intent_id FROM match_seen WHERE article_id IN ({ph})",
            chunk,
        ) as cur:
            for article_id, intent_id in await cur.fetchall():
                result[article_id].append(intent_id)
    return result


def _log_ttl_summary(
    *,
    cutoff_ts: int,
    archived: int,
    skipped_missing: int,
    poisoned_skipped: int,
    deleted_qdrant: int,
    deleted_fi: int,
    deleted_ms: int,
    aborted_stage: str,
    started_at: float,
    settings: Settings,
) -> None:
    """Single conservation-accounting line per run.

    Emitted on every exit path (including aborts) so the moved-vs-deleted
    reconciliation never has a silent gap; a completed run must show
    archived == deleted_qdrant.
    """
    elapsed_ms = int((monotonic() - started_at) * 1000)
    logger.info(
        "qdrant_ttl run: cutoff_ts=%d archived=%d skipped_missing=%d "
        "poisoned_skipped=%d deleted_qdrant=%d deleted_feed_items=%d "
        "deleted_match_seen=%d aborted_stage=%s elapsed_ms=%d interval_hours=%d",
        cutoff_ts,
        archived,
        skipped_missing,
        poisoned_skipped,
        deleted_qdrant,
        deleted_fi,
        deleted_ms,
        aborted_stage,
        elapsed_ms,
        settings.maintenance_interval_hours,
    )


async def _run_qdrant_ttl(qdrant_handle: QdrantHandle, settings: Settings) -> None:
    # Imported lazily (mirrors the qdrant_client.models convention) so this
    # module stays importable without the vector-store dependency chain.
    from sembr.vector_store.news_archive import (  # noqa: PLC0415
        build_archive_point,
        upsert_archive_points,
    )

    started_at = monotonic()
    cutoff_ts = int(time.time()) - settings.qdrant_news_retention_days * 86400

    archived = 0
    skipped_missing = 0
    poisoned_skipped = 0
    deleted_qdrant = 0
    deleted_fi = 0
    deleted_ms = 0
    aborted_stage = "none"

    def _summary() -> None:
        _log_ttl_summary(
            cutoff_ts=cutoff_ts,
            archived=archived,
            skipped_missing=skipped_missing,
            poisoned_skipped=poisoned_skipped,
            deleted_qdrant=deleted_qdrant,
            deleted_fi=deleted_fi,
            deleted_ms=deleted_ms,
            aborted_stage=aborted_stage,
            started_at=started_at,
            settings=settings,
        )

    try:
        purge_uuids = await _scroll_expired_uuids(qdrant_handle, cutoff_ts)
    except Exception:
        logger.warning("qdrant_ttl: scroll failed", exc_info=True)
        aborted_stage = "scroll"
        _summary()
        return

    if not purge_uuids:
        _summary()
        return

    if not settings.qdrant_archive_enabled:
        # Legacy pure-delete path, call-for-call identical to the pre-archive
        # implementation: bulk delete, then cascade.
        try:
            await _delete_qdrant_points(qdrant_handle, purge_uuids)
        except Exception:
            logger.warning("qdrant_ttl: delete failed", exc_info=True)
            aborted_stage = "delete"
            _summary()
            return
        deleted_qdrant = len(purge_uuids)
        try:
            deleted_fi, deleted_ms = await _cascade_delete_sqlite(purge_uuids)
        except Exception:
            # Orphan rows are swept by the reconcile job (feed_items via its
            # md5 scan, match_seen via its orphan sweep); the summary line
            # must still go out so the ledger has no silent gap.
            logger.warning("qdrant_ttl: sqlite cascade failed", exc_info=True)
            aborted_stage = "cascade"
        _summary()
        return

    for i in range(0, len(purge_uuids), _ARCHIVE_BATCH):
        batch = purge_uuids[i : i + _ARCHIVE_BATCH]

        try:
            points = await qdrant_handle.client.retrieve(
                collection_name=ALIAS_NAME,
                ids=batch,
                with_payload=True,
                with_vectors=True,
            )
        except Exception:
            # Equivalent to an upsert failure: nothing deleted yet, the
            # whole remainder is retried next interval.
            logger.warning("qdrant_ttl: archive retrieve failed", exc_info=True)
            aborted_stage = "retrieve"
            break

        # Ids the scroll saw but retrieve no longer finds (concurrent manual
        # prune): nothing to archive, nothing to delete — skip, don't abort.
        missing = len(batch) - len(points)
        if missing:
            skipped_missing += missing
            logger.warning(
                "qdrant_ttl: %d expired point(s) vanished between scroll and retrieve",
                missing,
            )

        point_ids = [str(p.id) for p in points]
        try:
            matched_by_id = await _fetch_matched_intents(point_ids)
        except Exception:
            # Nothing deleted yet — the whole remainder retries next interval.
            # A SQLite read failure must not swallow the run's ledger line.
            logger.warning("qdrant_ttl: match_seen lookup failed", exc_info=True)
            aborted_stage = "match_seen"
            break
        archived_at_ts = int(time.time())

        archive_points = []
        delete_ids: list[str] = []
        for point in points:
            pid = str(point.id)
            archive_point = build_archive_point(point, matched_by_id.get(pid, []), archived_at_ts)
            if archive_point is None:
                # Poisoned point (no usable vector): keep it in news_current
                # rather than stalling the whole run — losing it would break
                # the no-data-loss invariant, stalling would force operators
                # to disable archiving entirely.
                poisoned_skipped += 1
                logger.error(
                    "qdrant_ttl: point %s has no usable vector; leaving in "
                    "news_current (poisoned_skipped)",
                    pid,
                )
                continue
            archive_points.append(archive_point)
            delete_ids.append(pid)

        if archive_points:
            try:
                await upsert_archive_points(qdrant_handle.client, archive_points, wait=True)
            except Exception:
                logger.warning("qdrant_ttl: archive upsert failed", exc_info=True)
                aborted_stage = "upsert"
                break
            archived += len(archive_points)

        if delete_ids:
            try:
                await _delete_qdrant_points(qdrant_handle, delete_ids)
            except Exception:
                # Points now exist in both collections — benign: the hot
                # path only queries news_current, and the next run re-archives
                # (overwrite) then deletes.
                logger.warning("qdrant_ttl: delete failed", exc_info=True)
                aborted_stage = "delete"
                break
            deleted_qdrant += len(delete_ids)

            try:
                fi, ms = await _cascade_delete_sqlite(delete_ids)
            except Exception:
                # SQLite rows for already-deleted points linger until the
                # reconcile job sweeps them; the summary line must still go
                # out so the conservation ledger has no silent gap.
                logger.warning("qdrant_ttl: sqlite cascade failed", exc_info=True)
                aborted_stage = "cascade"
                break
            deleted_fi += fi
            deleted_ms += ms

        await asyncio.sleep(0)  # defence-in-depth yield between batches

    _summary()


def add_qdrant_ttl_job(
    scheduler: AsyncIOScheduler,
    qdrant_handle: QdrantHandle,
    settings: Settings,
) -> None:
    """Register the Qdrant TTL job with a 15-minute startup offset."""
    now = datetime.now(UTC)
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

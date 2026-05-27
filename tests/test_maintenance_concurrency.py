# SPDX-License-Identifier: Apache-2.0
"""Concurrency regression tests for the reconcile / TTL paths.

Two scenarios:

- ``test_changes_count_inside_txn`` — ``SELECT changes()`` must read inside
  the transaction so a concurrent writer between chunks can't bleed its
  rowcount into reconcile's accumulated deleted count.
- ``test_concurrent_writer_not_starved`` — with the ``idx_match_seen_article_id``
  index in place + chunk size 500, a normal collect_feed-style writer must
  drain in well under the perceived-lockup threshold.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.config import Settings
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.maintenance.reconcile import _run_reconcile
from sembr.maintenance.qdrant_ttl import _run_qdrant_ttl
from sembr.vector_store.news import md5_to_uuid


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    return conn


async def _seed_feed(conn) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t', 30)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
        return (await cur.fetchone())[0]


async def _seed_feed_items(conn, md5s: list[str], feed_id: int) -> None:
    """Bulk-insert feed_items in a single transaction (faster setup)."""
    async with conn.execute("BEGIN"):
        pass
    for m in md5s:
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (m, feed_id))
    await conn.commit()


def _make_qdrant_handle(found_md5s: set[str]) -> MagicMock:
    found_uuids = {md5_to_uuid(m) for m in found_md5s}

    async def fake_retrieve(*, collection_name, ids, **kwargs):
        out = []
        for uid in ids:
            if uid in found_uuids:
                p = MagicMock()
                p.id = uid
                out.append(p)
        return out

    handle = MagicMock()
    handle.client.retrieve = AsyncMock(side_effect=fake_retrieve)
    return handle


# ---------------------------------------------------------------------------
# SELECT changes() correctness across chunk boundary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_changes_count_not_polluted_by_concurrent_writer(caplog):
    """``SELECT changes()`` must run INSIDE the chunk's transaction.

    We verify this two ways:
      (1) Snapshot semantics — the racer's INSERT (landed between chunk
          commits) survives because reconcile only acts on the md5 snapshot
          taken in step 1.
      (2) Direct rowcount via the ``orphan_deleted=N`` INFO log — it must
          equal the snapshot size (700), not 701/702. If a future change
          moves ``SELECT changes()`` outside the txn, a writer that sneaks
          in between COMMIT and the changes() read would bleed its rowcount
          into the accumulator and flip the assertion.
    """
    import re

    conn = await _make_conn()
    feed_id = await _seed_feed(conn)
    # 700 md5s = 2 chunks (500 + 200) so we can race a writer between them.
    md5s = [f"{i:032x}" for i in range(700)]
    await _seed_feed_items(conn, md5s, feed_id)

    qdrant = _make_qdrant_handle(set())  # everything is orphan

    # Capture the original transaction context manager so we can wrap it.
    from sembr.db import sqlite as _sqlite

    original_transaction = _sqlite.transaction
    chunks_seen = 0
    inserted_during_race = "f" * 32

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def racing_transaction():
        nonlocal chunks_seen
        async with original_transaction() as txn:
            chunks_seen += 1
            yield txn
        # After the FIRST chunk commits, sneak in a concurrent INSERT before
        # reconcile re-acquires the lock for the next chunk.
        if chunks_seen == 1:
            async with original_transaction() as racer:
                await racer.execute(
                    "INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)",
                    (inserted_during_race, feed_id),
                )

    # Patch the reconcile module's `transaction` symbol (imported at top).
    import sembr.maintenance.reconcile as recon_mod

    recon_mod.transaction = racing_transaction
    try:
        with caplog.at_level("INFO", logger="sembr.maintenance.reconcile"):
            await _run_reconcile(qdrant, Settings())
    finally:
        recon_mod.transaction = original_transaction

    # (1) Snapshot semantics — racer survives, originally-orphan rows are gone.
    async with conn.execute("SELECT md5 FROM feed_items") as cur:
        remaining = {r[0] for r in await cur.fetchall()}
    assert remaining == {inserted_during_race}, (
        "the racer's INSERT must survive; reconcile must touch only the "
        "snapshot it scanned, not rows that arrived after the snapshot"
    )

    # (2) Direct guard — orphan_deleted reported by reconcile must equal
    #     exactly the snapshot size (700). Any value > 700 means SELECT
    #     changes() leaked a concurrent writer's rowcount.
    log_lines = [r.getMessage() for r in caplog.records]
    matches = [m for line in log_lines for m in re.findall(r"orphan_deleted=(\d+)", line)]
    assert matches, f"no orphan_deleted= count in reconcile log; got {log_lines!r}"
    deleted_reported = int(matches[-1])
    assert deleted_reported == 700, (
        f"reconcile reported orphan_deleted={deleted_reported}, expected 700. "
        "If SELECT changes() were moved outside the txn, a writer between "
        "COMMIT and changes() would bleed its rowcount into this counter."
    )

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


# ---------------------------------------------------------------------------
# 🟡-2 part 2: Risk #10 — index keeps writer drain time well below threshold
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_writer_not_starved_during_qdrant_ttl():
    """Risk #10: chunk size 500 + ``idx_match_seen_article_id`` keeps each
    cascade-delete txn short enough that a concurrent collect_feed-style
    writer drains within a perceptible bound.

    Precondition assertion guards the failure mode: without the index the
    writer would time out instead of failing fast on a missing precondition.
    """
    conn = await _make_conn()

    # Hard precondition: index must exist. This catches the root cause if
    # the test fails — without it a starvation failure looks like a flaky
    # latency assertion instead of "you broke the match_seen article_id index".
    async with conn.execute("PRAGMA index_list(match_seen)") as cur:
        names = {r[1] for r in await cur.fetchall()}
    assert (
        "idx_match_seen_article_id" in names
    ), "test prerequisite: idx_match_seen_article_id must exist"

    feed_id = await _seed_feed(conn)
    # Seed 1000 feed_items + an intent and 5000 match_seen rows so
    # cascade-delete actually has work per chunk.
    md5s = [f"{i:032x}" for i in range(1000)]
    await _seed_feed_items(conn, md5s, feed_id)

    await conn.execute(
        "INSERT INTO intents (id, name, text, threshold, schedule, channels, enabled) "
        "VALUES (1, 'i', 't', 0.75, '{\"mode\":\"event\"}', '[]', 1)"
    )
    await conn.commit()

    uuids = [md5_to_uuid(m) for m in md5s]
    async with conn.execute("BEGIN"):
        pass
    for u in uuids[: 5000 // 1]:
        # 1 intent × 1000 articles × 5 ≈ 5000 match_seen rows is overkill —
        # one row per article keeps the test fast yet exercises the join.
        if uuids.index(u) >= 1000:
            break
        await conn.execute("INSERT INTO match_seen (intent_id, article_id) VALUES (1, ?)", (u,))
    await conn.commit()

    # qdrant_ttl scroll returns all 1000 uuids (one page).
    points = []
    for u in uuids:
        p = MagicMock()
        p.id = u
        points.append(p)
    qdrant = MagicMock()
    qdrant.client.scroll = AsyncMock(return_value=(points, None))
    qdrant.client.delete = AsyncMock()

    # Race a writer that needs the lock during ttl_run.
    writer_acquired_at: list[float] = []
    writer_started_at: list[float] = []

    async def racing_writer():
        from sembr.db.sqlite import transaction

        # Wait one tick so qdrant_ttl can take the lock first.
        await asyncio.sleep(0.05)
        writer_started_at.append(time.monotonic())
        async with transaction() as txn:
            writer_acquired_at.append(time.monotonic())
            await txn.execute(
                "INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)",
                ("a" * 32, feed_id),
            )

    ttl_task = asyncio.create_task(_run_qdrant_ttl(qdrant, Settings()))
    writer_task = asyncio.create_task(racing_writer())
    await asyncio.gather(ttl_task, writer_task)

    assert writer_acquired_at, "writer never finished"
    assert writer_started_at, "writer never started"
    # Writer wait time = time to acquire the lock once it tried.
    wait_seconds = writer_acquired_at[0] - writer_started_at[0]
    # Threshold derivation:
    #   chunk=500 + match_seen article_id index → < 100ms per chunk worst-case.
    #   1000 rows = 2 chunks → at most ~200ms of contention.
    # We allow a 2× margin for CI variability.
    assert wait_seconds < 0.5, (
        f"writer took {wait_seconds:.3f}s to acquire the write lock — "
        f"chunk-scoped txn invariant or match_seen index regression?"
    )

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None

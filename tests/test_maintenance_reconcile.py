"""Unit tests for sembr.maintenance.reconcile (S1 + Risk row 1 + D3)."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.config import Settings
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.maintenance.reconcile import _run_reconcile
from sembr.vector_store.news import md5_to_uuid


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    return conn


async def _insert_feed(conn) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t', 30)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
        return (await cur.fetchone())[0]


async def _insert_feed_item(conn, md5: str, feed_id: int) -> None:
    await conn.execute(
        "INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id)
    )
    await conn.commit()


async def _insert_pending(conn, md5: str, feed_id: int) -> None:
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) "
        "VALUES (?, ?, 'u', 't', 'b')",
        (md5, feed_id),
    )
    await conn.commit()


def _make_qdrant_handle(found_md5s: set[str]) -> MagicMock:
    """Return a QdrantHandle whose retrieve() reports only `found_md5s` exist."""
    found_uuids = {md5_to_uuid(m) for m in found_md5s}

    async def fake_retrieve(*, collection_name, ids, **kwargs):
        # Only return the ids that map to a "found" md5; mimicking Qdrant's
        # missing-id-skip semantics rather than 404.
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


@pytest.mark.asyncio
async def test_reconcile_deletes_orphans():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5s = [f"{i:032x}" for i in range(100)]
    for m in md5s:
        await _insert_feed_item(conn, m, feed_id)

    # Qdrant has only the first 90 — last 10 are orphans
    qdrant = _make_qdrant_handle(set(md5s[:90]))

    await _run_reconcile(qdrant, Settings())

    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        remaining = (await cur.fetchone())[0]
    assert remaining == 90

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_reconcile_skips_pending():
    """Risk row 1: rows present in pending_articles must NOT be considered orphan
    even if Qdrant doesn't have a point for them yet (embedder is mid-flight).
    """
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5s = [f"{i:032x}" for i in range(100)]
    for m in md5s:
        await _insert_feed_item(conn, m, feed_id)
    # 5 of these are mid-embed (pending), Qdrant doesn't have them yet
    pending_md5s = md5s[:5]
    for m in pending_md5s:
        await _insert_pending(conn, m, feed_id)

    # Qdrant only has the 95 non-pending md5s
    qdrant = _make_qdrant_handle(set(md5s[5:]))

    await _run_reconcile(qdrant, Settings())

    # Pending rows must survive even though Qdrant didn't echo them
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        remaining = (await cur.fetchone())[0]
    assert remaining == 100  # nothing deleted

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_reconcile_idempotent():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5s = [f"{i:032x}" for i in range(50)]
    for m in md5s:
        await _insert_feed_item(conn, m, feed_id)

    qdrant = _make_qdrant_handle(set(md5s[:40]))

    await _run_reconcile(qdrant, Settings())
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        first_remaining = (await cur.fetchone())[0]
    assert first_remaining == 40

    # Second pass: no orphans left, must be a no-op
    await _run_reconcile(qdrant, Settings())
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        second_remaining = (await cur.fetchone())[0]
    assert second_remaining == 40

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_reconcile_logs_zero_when_empty(caplog):
    conn = await _make_conn()
    qdrant = _make_qdrant_handle(set())

    with caplog.at_level("INFO", logger="sembr.maintenance.reconcile"):
        await _run_reconcile(qdrant, Settings())

    # The "no rows" early-return path emits a log line with all zero counters.
    assert any(
        "scanned=0 found=0 orphan_deleted=0" in r.getMessage()
        for r in caplog.records
    )

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None

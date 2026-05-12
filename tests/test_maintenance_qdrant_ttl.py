"""Unit tests for sembr.maintenance.qdrant_ttl (S3 + S4 + S11 + D4)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.config import Settings
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
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


async def _insert_feed(conn) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t', 30)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
        return (await cur.fetchone())[0]


async def _seed_feed_item(conn, md5: str, feed_id: int) -> None:
    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.commit()


async def _seed_intent(conn, intent_id: int) -> None:
    await conn.execute(
        "INSERT INTO intents (id, name, text, threshold, schedule, channels, enabled) "
        "VALUES (?, 'i', 't', 0.75, '{\"mode\":\"event\"}', '[]', 1)",
        (intent_id,),
    )
    await conn.commit()


async def _seed_match_seen(conn, intent_id: int, article_uuid: str) -> None:
    await conn.execute(
        "INSERT INTO match_seen (intent_id, article_id) VALUES (?, ?)",
        (intent_id, article_uuid),
    )
    await conn.commit()


def _make_qdrant_handle(scroll_uuids: list[str]) -> MagicMock:
    """Returns a handle whose scroll() yields the given uuids in one page."""
    points = []
    for u in scroll_uuids:
        p = MagicMock()
        p.id = u
        points.append(p)

    async def fake_scroll(**kwargs):
        # Single-page result for tests; mirrors what Qdrant returns when
        # collection size < limit.
        return points, None

    handle = MagicMock()
    handle.client.scroll = AsyncMock(side_effect=fake_scroll)
    handle.client.delete = AsyncMock()
    return handle


@pytest.mark.asyncio
async def test_qdrant_ttl_cascade_delete():
    """50 expired Qdrant points → feed_items -50, match_seen rows for those
    article_ids -all (5 intents per article = 250 match_seen rows deleted)."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    # Seed 5 intents for the cascade match_seen
    for iid in range(1, 6):
        await _seed_intent(conn, iid)

    md5s = [f"{i:032x}" for i in range(50)]
    for m in md5s:
        await _seed_feed_item(conn, m, feed_id)
        u = md5_to_uuid(m)
        for iid in range(1, 6):
            await _seed_match_seen(conn, iid, u)

    expired_uuids = [md5_to_uuid(m) for m in md5s]
    qdrant = _make_qdrant_handle(expired_uuids)

    await _run_qdrant_ttl(qdrant, Settings())

    qdrant.client.delete.assert_awaited()  # at least one Qdrant delete batch
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        assert (await cur.fetchone())[0] == 0
    async with conn.execute("SELECT COUNT(*) FROM match_seen") as cur:
        assert (await cur.fetchone())[0] == 0

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_qdrant_ttl_no_expired(caplog):
    """No points → no Qdrant delete called, no SQLite delete, log writes zeros."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    await _seed_feed_item(conn, "a" * 32, feed_id)

    qdrant = _make_qdrant_handle([])

    with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
        await _run_qdrant_ttl(qdrant, Settings())

    qdrant.client.delete.assert_not_called()
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        assert (await cur.fetchone())[0] == 1
    assert any(
        "deleted_qdrant=0 deleted_feed_items=0 deleted_match_seen=0" in r.getMessage()
        for r in caplog.records
    )

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_qdrant_ttl_match_seen_only_for_targets():
    """match_seen rows belonging to a non-deleted article_id must NOT be
    pruned. Verifies the IN-list scoping (S11)."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    await _seed_intent(conn, 1)

    md5_old = "a" * 32
    md5_keep = "b" * 32
    await _seed_feed_item(conn, md5_old, feed_id)
    await _seed_feed_item(conn, md5_keep, feed_id)
    u_old = md5_to_uuid(md5_old)
    u_keep = md5_to_uuid(md5_keep)
    await _seed_match_seen(conn, 1, u_old)
    await _seed_match_seen(conn, 1, u_keep)

    # Only u_old is "expired"
    qdrant = _make_qdrant_handle([u_old])

    await _run_qdrant_ttl(qdrant, Settings())

    async with conn.execute("SELECT article_id FROM match_seen WHERE intent_id=1") as cur:
        kept = {r[0] for r in await cur.fetchall()}
    assert kept == {u_keep}

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None

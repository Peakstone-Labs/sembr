# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.maintenance.dead_ttl (S5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from sembr.config import Settings
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.maintenance.dead_ttl import _run_dead_ttl


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    return conn


async def _seed_dead(conn, md5: str, failed_at_iso: str, feed_id: int = 1) -> None:
    await conn.execute(
        "INSERT INTO dead_articles "
        "(md5, feed_id, url, title, body, published_at, error_message, failed_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (md5, feed_id, f"http://x/{md5[:6]}", "t", "b", None, "err", failed_at_iso),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_dead_ttl_basic_deletes_old_rows():
    conn = await _make_conn()
    now = datetime.now(timezone.utc)
    # 30 rows older than 14d, 70 fresh
    for i in range(30):
        old = (now - timedelta(days=20)).isoformat()
        await _seed_dead(conn, f"{i:032x}", old)
    for i in range(30, 100):
        fresh = (now - timedelta(days=2)).isoformat()
        await _seed_dead(conn, f"{i:032x}", fresh)

    settings = Settings(dead_articles_retention_days=14)
    await _run_dead_ttl(settings)

    async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
        remaining = (await cur.fetchone())[0]
    assert remaining == 70

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_dead_ttl_no_old_rows():
    conn = await _make_conn()
    now = datetime.now(timezone.utc)
    for i in range(5):
        fresh = (now - timedelta(days=1)).isoformat()
        await _seed_dead(conn, f"{i:032x}", fresh)

    settings = Settings(dead_articles_retention_days=14)
    await _run_dead_ttl(settings)

    async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
        remaining = (await cur.fetchone())[0]
    assert remaining == 5

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None

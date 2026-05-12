# SPDX-License-Identifier: Apache-2.0
"""D32 v1.1 — create_feed inherits last_collected_at from existing newsapi feeds.

A newly-added newsapi feed must NOT pull a 24h bootstrap window on its first
master tick — that would burn extra tokens and pollute an already-aligned
cohort. Tested in two regimes:

1. SC5: existing enabled newsapi feed in the DB → new newsapi feed copies
   the existing last_collected_at.
2. SC6: empty DB → new newsapi feed gets NULL (bootstrap path via
   _date_window now-1d).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite
import pytest

from sembr.db import sqlite as _sqlite_mod
from sembr.db.feeds import create_feed, init_feed_tables


@pytest.mark.asyncio
async def test_create_feed_newsapi_inherits_last_collected() -> None:
    """SC5: with an existing enabled newsapi feed having last_collected_at,
    a freshly created newsapi feed must inherit that timestamp so the
    'all enabled newsapi feeds share the same cursor' invariant (v1.0 D7)
    is preserved on insert."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        cursor_iso = "2026-05-09T12:34:56Z"
        # Pre-existing newsapi feed
        await conn.execute(
            "INSERT INTO feeds (name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Reuters", "reuters.com", "newsapi", cursor_iso, 1),
        )
        await conn.commit()

        new_feed = await create_feed(
            conn,
            name="BBC",
            url="bbc.com",
            source_type="newsapi",
            poll_interval_minutes=30,
        )
        assert new_feed.last_collected_at == cursor_iso
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_create_feed_newsapi_first_in_db_keeps_null() -> None:
    """SC6: empty newsapi cohort → new feed's last_collected_at stays NULL,
    falling back to _date_window now-1d on first master tick (bootstrap)."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        new_feed = await create_feed(
            conn,
            name="Reuters",
            url="reuters.com",
            source_type="newsapi",
        )
        assert new_feed.last_collected_at is None
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_create_feed_newsapi_skips_disabled_existing() -> None:
    """D32: only enabled+last_collected_at-NOT-NULL feeds are eligible
    donors. A disabled stale row must not seed the new feed.
    """
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        # Disabled donor — must NOT be picked up.
        await conn.execute(
            "INSERT INTO feeds (name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Old", "old.example.com", "newsapi", "2020-01-01T00:00:00Z", 0),
        )
        await conn.commit()
        new_feed = await create_feed(
            conn,
            name="BBC",
            url="bbc.com",
            source_type="newsapi",
        )
        assert new_feed.last_collected_at is None
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_create_feed_newsapi_picks_max_when_cohort_desynced() -> None:
    """Loop 6 💡-4: when the cohort is temporarily desynced (e.g. partial
    failure on a previous tick advanced cursor for one feed but not the
    other), the donor query picks the MAX (most recent) cursor — safest
    'we've seen everything before this' assumption."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        old_cursor = "2026-05-01T00:00:00Z"
        new_cursor = "2026-05-09T12:00:00Z"
        # Two donors: one stale, one freshly advanced (cohort desync state).
        await conn.execute(
            "INSERT INTO feeds (name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Old", "old.example.com", "newsapi", old_cursor, 1),
        )
        await conn.execute(
            "INSERT INTO feeds (name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Fresh", "fresh.example.com", "newsapi", new_cursor, 1),
        )
        await conn.commit()

        new_feed = await create_feed(
            conn,
            name="Reuters",
            url="reuters.com",
            source_type="newsapi",
        )
        # Must inherit the MAX (newer) cursor, not whichever row SQLite
        # happens to return first.
        assert new_feed.last_collected_at == new_cursor
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_create_feed_rss_unaffected_by_newsapi_donor() -> None:
    """Regression guard: rss feeds must not inherit a newsapi cursor — the
    D32 branch is gated on source_type=='newsapi'."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        await conn.execute(
            "INSERT INTO feeds (name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Reuters", "reuters.com", "newsapi", "2026-05-09T00:00:00Z", 1),
        )
        await conn.commit()
        rss_feed = await create_feed(
            conn,
            name="Hacker News",
            url="https://hnrss.org/frontpage",
            source_type="rss",
        )
        assert rss_feed.last_collected_at is None
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()

"""Tests for feeds DB migration — enabled column idempotent add (C1)."""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from sembr.db import sqlite as _sqlite_mod
from sembr.db.feeds import (
    _ensure_enabled_column,  # type: ignore[attr-defined]
    init_feed_tables,
    get_feed,
    update_feed,
)


async def _insert_feed(conn: aiosqlite.Connection, name: str, url: str) -> int:
    """Insert a feed row directly and return its id."""
    cursor = await conn.execute(
        "INSERT INTO feeds (name, url) VALUES (?, ?)",
        (name, url),
    )
    await conn.commit()
    return cursor.lastrowid  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_fresh_db_has_enabled_column() -> None:
    """init_feed_tables on a new DB creates feeds with enabled column."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_feed_tables(conn)
        async with conn.execute("PRAGMA table_info(feeds)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "enabled" in cols
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_adds_enabled_to_old_db() -> None:
    """Old DB without enabled column gets it added with DEFAULT 1 for all rows."""
    conn = await aiosqlite.connect(":memory:")
    try:
        # Simulate old schema without enabled column
        await conn.execute("""
            CREATE TABLE feeds (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                name                  TEXT NOT NULL,
                url                   TEXT NOT NULL UNIQUE,
                source_type           TEXT NOT NULL DEFAULT 'rss',
                config                TEXT NOT NULL DEFAULT '{}',
                poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
                last_collected_at     TEXT,
                created_at            TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            "INSERT INTO feeds (name, url) VALUES (?, ?)",
            ("old-feed", "https://example.com/rss"),
        )
        await conn.commit()

        await init_feed_tables(conn)

        async with conn.execute("PRAGMA table_info(feeds)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        assert "enabled" in cols

        async with conn.execute("SELECT enabled FROM feeds WHERE url=?", ("https://example.com/rss",)) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == 1
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_init_feed_tables_idempotent() -> None:
    """Calling init_feed_tables twice does not raise."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_feed_tables(conn)
        await init_feed_tables(conn)
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_new_feed_default_enabled_true() -> None:
    """Rows inserted without specifying enabled default to 1 (True)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_feed_tables(conn)
        feed_id = await _insert_feed(conn, "test", "https://example.com/feed")
        feed = await get_feed(conn, feed_id)
        assert feed is not None
        assert feed.enabled is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_update_feed_enabled_false() -> None:
    """update_feed can set enabled=False."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        feed_id = await _insert_feed(conn, "test", "https://example.com/feed")
        updated = await update_feed(conn, feed_id, enabled=False)
        assert updated is not None
        assert updated.enabled is False
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_update_feed_name_and_tags() -> None:
    """update_feed updates name and tags atomically."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        feed_id = await _insert_feed(conn, "original", "https://example.com/feed")
        updated = await update_feed(conn, feed_id, tags=["news", "tech"], name="renamed")
        assert updated is not None
        assert updated.name == "renamed"
        assert set(updated.tags) == {"news", "tech"}
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_update_feed_nonexistent_returns_none() -> None:
    """update_feed returns None for a feed_id that doesn't exist."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        result = await update_feed(conn, 9999, name="ghost")
        assert result is None
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_update_feed_tags_only_nonexistent_returns_none() -> None:
    """update_feed returns None (not IntegrityError) for tags-only update on missing feed."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        result = await update_feed(conn, 9999, tags=["news"])
        assert result is None
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()


@pytest.mark.asyncio
async def test_update_feed_rejects_non_updatable_fields() -> None:
    """update_feed raises ValueError if caller tries to update url or source_type."""
    conn = await aiosqlite.connect(":memory:")
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    try:
        await init_feed_tables(conn)
        feed_id = await _insert_feed(conn, "test", "https://example.com/feed")
        with pytest.raises(ValueError, match="non-updatable"):
            await update_feed(conn, feed_id, url="https://evil.com")
    finally:
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None
        await conn.close()

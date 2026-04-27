"""Feed and feed_item persistence.

DDL is idempotent (CREATE TABLE IF NOT EXISTS).  All functions accept the
global aiosqlite connection returned by get_conn() so callers don't open
their own connections.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from sembr.collector.initial_feeds import INITIAL_FEEDS
from sembr.models import Feed

_CREATE_FEEDS = """
CREATE TABLE IF NOT EXISTS feeds (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    name                  TEXT NOT NULL,
    url                   TEXT NOT NULL UNIQUE,
    source_type           TEXT NOT NULL DEFAULT 'rss',
    config                TEXT NOT NULL DEFAULT '{}',
    poll_interval_minutes INTEGER NOT NULL DEFAULT 30,
    last_collected_at     TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_FEED_ITEMS = """
CREATE TABLE IF NOT EXISTS feed_items (
    md5          TEXT PRIMARY KEY,
    feed_id      INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    collected_at TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_feed_items_feed_id ON feed_items(feed_id)
"""


async def init_feed_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_FEEDS)
    await conn.execute(_CREATE_FEED_ITEMS)
    await conn.execute(_CREATE_IDX)
    await conn.commit()


async def seed_initial_feeds(conn: aiosqlite.Connection) -> int:
    """Seed INITIAL_FEEDS on first startup (when feeds table is empty).

    Seed-once semantics: if any rows exist we do nothing, so feeds deleted via
    the API never come back on restart (satisfies SC3 / 已删源重启不复现).
    Returns count of rows inserted (0 if table already had rows).
    """
    async with conn.execute("SELECT COUNT(*) FROM feeds") as cur:
        row = await cur.fetchone()
    if row and row[0] > 0:
        return 0

    count = 0
    for f in INITIAL_FEEDS:
        cursor = await conn.execute(
            "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES (?, ?, ?)",
            (f["name"], f["url"], f["poll_interval_minutes"]),
        )
        count += cursor.rowcount
    await conn.commit()
    return count


def _row_to_feed(row: aiosqlite.Row) -> Feed:
    return Feed(
        id=row[0],
        name=row[1],
        url=row[2],
        source_type=row[3],
        config=json.loads(row[4]),
        poll_interval_minutes=row[5],
        last_collected_at=row[6],
        created_at=row[7],
    )


async def create_feed(
    conn: aiosqlite.Connection,
    name: str,
    url: str,
    source_type: str = "rss",
    config: dict | None = None,
    poll_interval_minutes: int = 30,
) -> Feed:
    cursor = await conn.execute(
        """INSERT INTO feeds (name, url, source_type, config, poll_interval_minutes)
           VALUES (?, ?, ?, ?, ?)""",
        (name, str(url), source_type, json.dumps(config or {}), poll_interval_minutes),
    )
    await conn.commit()
    feed_id = cursor.lastrowid
    return await get_feed(conn, feed_id)  # type: ignore[arg-type]


async def list_feeds(conn: aiosqlite.Connection) -> list[Feed]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute("SELECT id,name,url,source_type,config,poll_interval_minutes,last_collected_at,created_at FROM feeds") as cur:
        rows = await cur.fetchall()
    return [_row_to_feed(r) for r in rows]


async def get_feed(conn: aiosqlite.Connection, feed_id: int) -> Feed | None:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT id,name,url,source_type,config,poll_interval_minutes,last_collected_at,created_at FROM feeds WHERE id=?",
        (feed_id,),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_feed(row) if row else None


async def delete_feed(conn: aiosqlite.Connection, feed_id: int) -> bool:
    cursor = await conn.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def fingerprint_exists(conn: aiosqlite.Connection, md5: str) -> bool:
    async with conn.execute("SELECT 1 FROM feed_items WHERE md5=?", (md5,)) as cur:
        return await cur.fetchone() is not None


async def insert_fingerprint(conn: aiosqlite.Connection, md5: str, feed_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO feed_items (md5, feed_id) VALUES (?, ?)",
        (md5, feed_id),
    )
    await conn.commit()


async def update_last_collected(conn: aiosqlite.Connection, feed_id: int) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    await conn.execute(
        "UPDATE feeds SET last_collected_at=? WHERE id=?",
        (now, feed_id),
    )
    await conn.commit()

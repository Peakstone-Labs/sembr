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

# Tracks URLs that were ever seeded from INITIAL_FEEDS — NOT all feed URLs.
# Scope: INITIAL_FEEDS entries only; user-POST feeds are not written here.
# Invariant: once a URL is in seeded_feeds it stays forever, even if the feed
# row is deleted, so auto-seed never revives a user-deleted feed (SC3).
_CREATE_SEEDED_INITIAL_FEEDS = """
CREATE TABLE IF NOT EXISTS seeded_feeds (
    url TEXT PRIMARY KEY
)
"""

_CREATE_IDX = """
CREATE INDEX IF NOT EXISTS idx_feed_items_feed_id ON feed_items(feed_id)
"""


async def init_feed_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_FEEDS)
    await conn.execute(_CREATE_FEED_ITEMS)
    await conn.execute(_CREATE_SEEDED_INITIAL_FEEDS)
    await conn.execute(_CREATE_IDX)
    await conn.commit()


async def seed_initial_feeds(conn: aiosqlite.Connection) -> int:
    """Seed INITIAL_FEEDS entries not yet recorded in seeded_feeds.

    seeded_feeds tracks every INITIAL_FEEDS URL ever seeded:
    - User deletes feed → seeded_feeds still has URL → not re-inserted (SC3)
    - Dev adds new entry to INITIAL_FEEDS → seeded_feeds missing URL → seeded on next startup
    """
    async with conn.execute("SELECT url FROM seeded_feeds") as cur:
        seeded = {row[0] for row in await cur.fetchall()}
    to_seed = [f for f in INITIAL_FEEDS if f["url"] not in seeded]

    for f in to_seed:
        await conn.execute(
            "INSERT OR IGNORE INTO feeds (name, url, poll_interval_minutes) VALUES (?, ?, ?)",
            (f["name"], f["url"], f["poll_interval_minutes"]),
        )
        await conn.execute("INSERT OR IGNORE INTO seeded_feeds (url) VALUES (?)", (f["url"],))
    await conn.commit()
    return len(to_seed)


def _row_to_feed(row: tuple) -> Feed:
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

_SELECT_FEEDS = "SELECT id,name,url,source_type,config,poll_interval_minutes,last_collected_at,created_at FROM feeds"


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
        (name, url, source_type, json.dumps(config or {}), poll_interval_minutes),
    )
    await conn.commit()
    feed_id = cursor.lastrowid
    return await get_feed(conn, feed_id)  # type: ignore[arg-type]


async def list_feeds(conn: aiosqlite.Connection) -> list[Feed]:
    async with conn.execute(_SELECT_FEEDS) as cur:
        rows = await cur.fetchall()
    return [_row_to_feed(r) for r in rows]


async def get_feed(conn: aiosqlite.Connection, feed_id: int) -> Feed | None:
    async with conn.execute(
        _SELECT_FEEDS + " WHERE id=?",
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

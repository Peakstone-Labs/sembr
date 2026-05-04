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
from sembr.db.feed_tags import (
    get_tags,
    init_feed_tag_tables,
    insert_tags_in_tx,
    list_all_tags,
)
from sembr.db.sqlite import transaction
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
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    enabled               INTEGER NOT NULL DEFAULT 1
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


async def _ensure_enabled_column(conn: aiosqlite.Connection) -> None:
    """C1: idempotent migration — add enabled column to existing DB."""
    async with conn.execute("PRAGMA table_info(feeds)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "enabled" not in cols:
        await conn.execute(
            "ALTER TABLE feeds ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1"
        )
        await conn.commit()


async def init_feed_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_FEEDS)
    await conn.execute(_CREATE_FEED_ITEMS)
    await conn.execute(_CREATE_SEEDED_INITIAL_FEEDS)
    await conn.execute(_CREATE_IDX)
    await conn.commit()
    await _ensure_enabled_column(conn)
    await init_feed_tag_tables(conn)


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
        enabled=bool(row[8]),
    )

_SELECT_FEEDS = "SELECT id,name,url,source_type,config,poll_interval_minutes,last_collected_at,created_at,enabled FROM feeds"


async def create_feed(
    conn: aiosqlite.Connection,
    name: str,
    url: str,
    source_type: str = "rss",
    config: dict | None = None,
    poll_interval_minutes: int = 30,
    tags: list[str] | None = None,
) -> Feed:
    # feeds insert + feed_tags insert must be atomic so a partial failure can't
    # leave a tagless feed (see api/feeds.post_feed rollback path).
    async with transaction() as txn:
        cursor = await txn.execute(
            """INSERT INTO feeds (name, url, source_type, config, poll_interval_minutes)
               VALUES (?, ?, ?, ?, ?)""",
            (name, url, source_type, json.dumps(config or {}), poll_interval_minutes),
        )
        feed_id = cursor.lastrowid
        if tags:
            await insert_tags_in_tx(txn, feed_id, tags)  # type: ignore[arg-type]
    feed = await get_feed(conn, feed_id)  # type: ignore[arg-type]
    assert feed is not None  # just inserted
    return feed


async def list_feeds(conn: aiosqlite.Connection) -> list[Feed]:
    """Lightweight: no tags fetched. Use for scheduler bootstrap (lifespan startup).

    For API responses that must include tags, call list_feeds_with_tags() instead.
    (Loop 2 review #🟡-3 — splits the per-call full-table feed_tags scan.)
    """
    async with conn.execute(_SELECT_FEEDS) as cur:
        rows = await cur.fetchall()
    return [_row_to_feed(r) for r in rows]


async def list_feeds_with_tags(conn: aiosqlite.Connection) -> list[Feed]:
    """list_feeds + populate tags via a single feed_tags scan (no N+1)."""
    feeds = await list_feeds(conn)
    if not feeds:
        return feeds
    tag_map = await list_all_tags(conn)
    for f in feeds:
        f.tags = tag_map.get(f.id, [])
    return feeds


async def get_feed_names(
    conn: aiosqlite.Connection, feed_ids: list[int]
) -> dict[int, str]:
    """Resolve feed_ids → feed.name. Missing ids are simply absent from the result."""
    if not feed_ids:
        return {}
    placeholders = ",".join("?" for _ in feed_ids)
    async with conn.execute(
        f"SELECT id,name FROM feeds WHERE id IN ({placeholders})",
        list(feed_ids),
    ) as cur:
        rows = await cur.fetchall()
    return {int(r[0]): str(r[1]) for r in rows}


async def get_feed(conn: aiosqlite.Connection, feed_id: int) -> Feed | None:
    async with conn.execute(
        _SELECT_FEEDS + " WHERE id=?",
        (feed_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    feed = _row_to_feed(row)
    feed.tags = await get_tags(conn, feed_id)
    return feed


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
    async with transaction() as txn:
        await txn.execute(
            "UPDATE feeds SET last_collected_at=? WHERE id=?",
            (now, feed_id),
        )


_UPDATABLE_FEED_COLS = frozenset({"name", "config", "poll_interval_minutes", "enabled"})


async def update_feed(
    conn: aiosqlite.Connection,
    feed_id: int,
    tags: list[str] | None = None,
    **fields,
) -> Feed | None:
    """Partial update of a feed row.

    Pass only the fields to change; unknown or non-updatable fields raise ValueError.
    tags are handled separately via feed_tags tables. Returns the updated Feed or
    None if feed_id not found.
    """
    bad = set(fields) - _UPDATABLE_FEED_COLS
    if bad:
        raise ValueError(f"non-updatable fields: {bad}")

    if fields or tags is not None:
        async with transaction() as txn:
            # Existence check inside the write lock so a concurrent delete_feed()
            # cannot slip between our check and the tag insert (TOCTOU prevention).
            # An empty BEGIN/COMMIT when feed is absent is a negligible cost.
            async with txn.execute("SELECT 1 FROM feeds WHERE id=?", (feed_id,)) as cur:
                if await cur.fetchone() is None:
                    return None
            if fields:
                set_clauses = ", ".join(f"{col}=?" for col in fields)
                values = []
                for col, val in fields.items():
                    values.append(json.dumps(val) if col == "config" else val)
                values.append(feed_id)
                await txn.execute(
                    f"UPDATE feeds SET {set_clauses} WHERE id=?", values
                )
            if tags is not None:
                await txn.execute("DELETE FROM feed_tags WHERE feed_id=?", (feed_id,))
                if tags:
                    await insert_tags_in_tx(txn, feed_id, tags)

    return await get_feed(conn, feed_id)

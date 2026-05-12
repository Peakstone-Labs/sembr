"""feed_tags persistence.

Composite PK (feed_id, tag) with FK CASCADE on feed_id so DELETE FROM feeds
auto-cleans tags. PRAGMA foreign_keys=ON is set globally in sqlite.py:19.
"""

from __future__ import annotations

import aiosqlite

_CREATE_FEED_TAGS = """
CREATE TABLE IF NOT EXISTS feed_tags (
    feed_id INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    tag     TEXT    NOT NULL,
    PRIMARY KEY (feed_id, tag)
)
"""

_CREATE_FEED_TAGS_IDX = """
CREATE INDEX IF NOT EXISTS idx_feed_tags_tag ON feed_tags(tag, feed_id)
"""


async def init_feed_tag_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_FEED_TAGS)
    await conn.execute(_CREATE_FEED_TAGS_IDX)
    await conn.commit()


async def insert_tags_in_tx(conn: aiosqlite.Connection, feed_id: int, tags: list[str]) -> None:
    """Insert tags as part of a caller-managed transaction. Caller must commit."""
    if not tags:
        return
    await conn.executemany(
        "INSERT OR IGNORE INTO feed_tags (feed_id, tag) VALUES (?, ?)",
        [(feed_id, t) for t in tags],
    )


async def replace_tags_in_tx(conn: aiosqlite.Connection, feed_id: int, tags: list[str]) -> None:
    """Replace the full tag set for one feed inside an open transaction."""
    await conn.execute("DELETE FROM feed_tags WHERE feed_id=?", (feed_id,))
    if tags:
        await conn.executemany(
            "INSERT OR IGNORE INTO feed_tags (feed_id, tag) VALUES (?, ?)",
            [(feed_id, t) for t in tags],
        )


async def get_tags(conn: aiosqlite.Connection, feed_id: int) -> list[str]:
    async with conn.execute(
        "SELECT tag FROM feed_tags WHERE feed_id=? ORDER BY tag", (feed_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def list_all_tags(conn: aiosqlite.Connection) -> dict[int, list[str]]:
    """Return {feed_id: [tag, ...]} for every feed_id with at least one tag."""
    async with conn.execute("SELECT feed_id, tag FROM feed_tags ORDER BY feed_id, tag") as cur:
        rows = await cur.fetchall()
    out: dict[int, list[str]] = {}
    for feed_id, tag in rows:
        out.setdefault(int(feed_id), []).append(tag)
    return out

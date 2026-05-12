"""pending_articles and dead_articles persistence.

Row presence in pending_articles is the *only* state indicator — no status column.
Dead articles are kept for forensics even after their feed is deleted (no FK cascade).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import aiosqlite

from sembr.collector.base import RawArticle
from sembr.db.sqlite import transaction

logger = logging.getLogger(__name__)

_BODY_CAP_BYTES = 1_048_576  # 1 MB sanity cap
_MD5_RE = re.compile(r"^[0-9a-f]{32}$")

_CREATE_PENDING = """
CREATE TABLE IF NOT EXISTS pending_articles (
    md5            TEXT PRIMARY KEY,
    feed_id        INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    published_at   TEXT,
    retry_count    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_DEAD = """
CREATE TABLE IF NOT EXISTS dead_articles (
    md5            TEXT PRIMARY KEY,
    feed_id        INTEGER,
    url            TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL,
    published_at   TEXT,
    error_message  TEXT,
    failed_at      TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_CREATE_IDX_PENDING_FEED = (
    "CREATE INDEX IF NOT EXISTS idx_pending_articles_feed_id ON pending_articles(feed_id)"
)
_CREATE_IDX_PENDING_RETRY = (
    # Index on retry_count covers WHERE retry_count < ?; SQLite implicitly stores rowid as
    # the B-tree secondary key, so ORDER BY rowid within the filtered set costs a sort over
    # at most BATCH_SIZE * max_retry rows — acceptable at MVP scale.
    # The old (retry_count, md5) index was useless because md5 TEXT has no relation to rowid order.
    "CREATE INDEX IF NOT EXISTS idx_pending_articles_retry ON pending_articles(retry_count)"
)
_CREATE_IDX_DEAD_FAILED = (
    "CREATE INDEX IF NOT EXISTS idx_dead_articles_failed_at ON dead_articles(failed_at)"
)


@dataclass
class PendingRow:
    md5: str
    feed_id: int
    url: str
    title: str
    body: str
    published_at: str | None
    retry_count: int


async def init_article_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_PENDING)
    await conn.execute(_CREATE_DEAD)
    await conn.execute(_CREATE_IDX_PENDING_FEED)
    # Drop old indexes that no longer match the current plan (schema migrations)
    await conn.execute("DROP INDEX IF EXISTS idx_pending_articles_retry_id")
    await conn.execute("DROP INDEX IF EXISTS idx_pending_articles_retry_rowid")
    await conn.execute(_CREATE_IDX_PENDING_RETRY)
    await conn.execute(_CREATE_IDX_DEAD_FAILED)
    await conn.commit()


async def insert_article_pending(
    conn: aiosqlite.Connection, article: RawArticle, feed_id: int
) -> bool:
    """Atomically insert fingerprint + pending row. Returns True iff newly inserted.

    Single BEGIN/COMMIT transaction ensures feed_items and pending_articles are
    always consistent — prevents an article slipping between dedup check and buffer write.
    Uses SELECT changes() rather than cursor.rowcount (aiosqlite rowcount is unreliable).
    """
    if not _MD5_RE.match(article.feed_md5):
        raise ValueError(f"feed_md5 must be 32 lowercase hex chars, got {article.feed_md5!r}")
    async with transaction() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO feed_items (md5, feed_id) VALUES (?, ?)",
            (article.feed_md5, feed_id),
        )
        async with conn.execute("SELECT changes()") as cur:
            n = (await cur.fetchone())[0]
        if n == 0:
            return False  # empty COMMIT is a no-op; no row was actually changed
        if len(article.body) > _BODY_CAP_BYTES:
            logger.info(
                "article body truncated: feed_id=%d md5=%s original=%d bytes cap=%d",
                feed_id,
                article.feed_md5,
                len(article.body),
                _BODY_CAP_BYTES,
            )
        body_capped = article.body[:_BODY_CAP_BYTES]
        await conn.execute(
            "INSERT INTO pending_articles (md5, feed_id, url, title, body, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                article.feed_md5,
                feed_id,
                article.url,
                article.title,
                body_capped,
                article.published_at.isoformat() if article.published_at else None,
            ),
        )
        return True


async def pull_pending_batch(
    conn: aiosqlite.Connection, batch_size: int, max_retry: int
) -> list[PendingRow]:
    # ORDER BY rowid gives true insertion order (FIFO) regardless of TEXT PK.
    async with conn.execute(
        "SELECT md5, feed_id, url, title, body, published_at, retry_count "
        "FROM pending_articles WHERE retry_count < ? ORDER BY rowid LIMIT ?",
        (max_retry, batch_size),
    ) as cur:
        rows = await cur.fetchall()
    return [PendingRow(*r) for r in rows]


async def increment_retry(conn: aiosqlite.Connection, md5s: list[str]) -> None:
    if not md5s:
        return
    placeholders = ",".join("?" * len(md5s))
    async with transaction() as conn:
        await conn.execute(
            f"UPDATE pending_articles SET retry_count = retry_count + 1 WHERE md5 IN ({placeholders})",
            md5s,
        )


async def delete_pending(conn: aiosqlite.Connection, md5s: list[str]) -> None:
    if not md5s:
        return
    placeholders = ",".join("?" * len(md5s))
    async with transaction() as conn:
        await conn.execute(
            f"DELETE FROM pending_articles WHERE md5 IN ({placeholders})",
            md5s,
        )


async def demote_to_dead(conn: aiosqlite.Connection, max_retry: int, error_message: str) -> int:
    """Atomically move ALL rows with retry_count >= max_retry to dead_articles.

    Use demote_md5s_to_dead when you need per-batch error attribution.
    This function is a global cleanup fallback for zombie rows.
    INSERT OR REPLACE preserves the latest failed_at and error_message if
    a row was previously demoted (🔴-1 fix: OR IGNORE silently lost re-queued rows).
    """
    async with transaction() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO dead_articles "
            "(md5, feed_id, url, title, body, published_at, error_message, failed_at) "
            "SELECT md5, feed_id, url, title, body, published_at, ?, datetime('now') "
            "FROM pending_articles WHERE retry_count >= ?",
            (error_message, max_retry),
        )
        await conn.execute(
            "DELETE FROM pending_articles WHERE retry_count >= ?",
            (max_retry,),
        )
        async with conn.execute("SELECT changes()") as cur:
            count = (await cur.fetchone())[0]
        return count


async def demote_md5s_to_dead(
    conn: aiosqlite.Connection, md5s: list[str], error_message: str
) -> int:
    """Atomically demote specific md5s to dead_articles with the exact error that caused them.

    Use this (not demote_to_dead) from the embedder worker so each demoted row
    carries the exception that actually exhausted its retries (🔴-2 fix).
    """
    if not md5s:
        return 0
    placeholders = ",".join("?" * len(md5s))
    async with transaction() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO dead_articles "
            "(md5, feed_id, url, title, body, published_at, error_message, failed_at) "
            f"SELECT md5, feed_id, url, title, body, published_at, ?, datetime('now') "
            f"FROM pending_articles WHERE md5 IN ({placeholders})",
            [error_message, *md5s],
        )
        await conn.execute(
            f"DELETE FROM pending_articles WHERE md5 IN ({placeholders})",
            md5s,
        )
        async with conn.execute("SELECT changes()") as cur:
            count = (await cur.fetchone())[0]
        return count

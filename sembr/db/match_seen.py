"""match_seen table: deduplication log for matched (intent, article) pairs.

D10: composite PK (intent_id, article_id) with ON DELETE CASCADE keeps cleanup trivial.
D11: INSERT OR IGNORE + RETURNING identifies newly inserted rows in a single statement.
"""
from __future__ import annotations

import aiosqlite

from sembr.db.sqlite import transaction

_CREATE_MATCH_SEEN = """
CREATE TABLE IF NOT EXISTS match_seen (
    intent_id        INTEGER NOT NULL,
    article_id       TEXT    NOT NULL,
    first_matched_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (intent_id, article_id),
    FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE
)
"""


async def init_match_seen_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_MATCH_SEEN)
    await conn.commit()


async def insert_unseen_returning_new(
    conn: aiosqlite.Connection,
    intent_id: int,
    article_ids: list[str],
) -> list[str]:
    """Insert (intent_id, article_id) pairs; return only the newly inserted article_ids.

    Uses a single multi-row INSERT OR IGNORE … RETURNING (D11) so the whole batch
    lands in one statement instead of N round-trips. RETURNING yields rows only for
    rows actually inserted; already-seen pairs are silently skipped by OR IGNORE.
    SQLite 3.35+ multi-row RETURNING is safe at any batch size within
    SQLITE_MAX_VARIABLE_NUMBER (32 766) — the MVP _SEARCH_LIMIT of 100 produces
    200 bound parameters, well below the limit.
    """
    if not article_ids:
        return []
    placeholders = ",".join(["(?,?)"] * len(article_ids))
    params = [v for aid in article_ids for v in (intent_id, aid)]
    async with transaction() as txn:
        async with txn.execute(
            # noqa: S608 — not a SQL injection risk: `placeholders` is "(?,?)" * n,
            # built entirely from a fixed template with no user-supplied content.
            f"INSERT OR IGNORE INTO match_seen (intent_id, article_id)"
            f" VALUES {placeholders} RETURNING article_id",
            params,
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def clear_intent(conn: aiosqlite.Connection, intent_id: int) -> None:
    """Delete all match_seen rows for an intent.

    Called when intent text changes (D4): the intent vector is re-embedded, so
    previously seen articles are no longer semantically de-duplicated against the
    new query vector and must be re-evaluated.
    """
    async with transaction() as txn:
        await txn.execute("DELETE FROM match_seen WHERE intent_id=?", (intent_id,))

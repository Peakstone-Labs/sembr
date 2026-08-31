# SPDX-License-Identifier: Apache-2.0
"""match_seen table: deduplication log for matched (intent, article) pairs.

Composite PK (intent_id, article_id) with ON DELETE CASCADE keeps cleanup trivial.
Inserts use INSERT OR IGNORE + RETURNING so newly inserted rows are identified in
a single round-trip.
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

# Reconcile/TTL/manual-prune all DELETE WHERE article_id IN (...) without an
# intent_id predicate; the composite PK leads with intent_id so without this
# helper index the planner falls back to a full scan and a 500-row chunk takes
# seconds, monopolising _WRITE_LOCK and stalling the ingest pipeline.
_CREATE_IDX_MATCH_SEEN_ARTICLE = (
    "CREATE INDEX IF NOT EXISTS idx_match_seen_article_id ON match_seen(article_id)"
)


# SQLite's SQLITE_MAX_VARIABLE_NUMBER is 32766; chunking the read queries at
# 500 keeps each statement far below it and matches the write-side chunk used
# by the TTL cascade, so a single oversized caller list can never turn into a
# hard error at the driver.
_READ_CHUNK = 500


async def init_match_seen_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_MATCH_SEEN)
    await conn.execute(_CREATE_IDX_MATCH_SEEN_ARTICLE)
    await conn.commit()


async def insert_unseen_returning_new(
    conn: aiosqlite.Connection,
    intent_id: int,
    article_ids: list[str],
) -> list[str]:
    """Insert (intent_id, article_id) pairs; return only the newly inserted article_ids.

    Uses a single multi-row INSERT OR IGNORE … RETURNING so the whole batch lands in
    one statement instead of N round-trips. RETURNING yields rows only for rows
    actually inserted; already-seen pairs are silently skipped by OR IGNORE.
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

    Called when intent text changes: the intent vector is re-embedded, so previously
    seen articles are no longer semantically de-duplicated against the new query
    vector and must be re-evaluated.
    """
    async with transaction() as txn:
        await txn.execute("DELETE FROM match_seen WHERE intent_id=?", (intent_id,))


async def article_ids_for_intents(
    conn: aiosqlite.Connection,
    intent_ids: list[int],
) -> list[str]:
    """DISTINCT ``article_id`` for every article any of ``intent_ids`` matched.

    ``article_id`` IS the Qdrant point uuid (see ``maintenance/qdrant_ttl.py``),
    so the returned list feeds a ``HasIdCondition`` directly — this is how the
    unified search endpoint filters ``news_current`` by intent, where the
    matched-intent history lives in SQLite rather than in the point payload.

    Sorted so the resulting filter (and therefore any test asserting on it) is
    deterministic. An empty ``intent_ids`` yields an empty list; callers must
    treat that as "restrict to nothing", never as "no filter".
    """
    if not intent_ids:
        return []
    found: set[str] = set()
    for i in range(0, len(intent_ids), _READ_CHUNK):
        chunk = intent_ids[i : i + _READ_CHUNK]
        ph = ",".join("?" * len(chunk))
        async with conn.execute(
            f"SELECT DISTINCT article_id FROM match_seen WHERE intent_id IN ({ph})",
            chunk,
        ) as cur:
            found.update(r[0] for r in await cur.fetchall())
    return sorted(found)


async def matched_intents_for_articles(
    conn: aiosqlite.Connection,
    article_ids: list[str],
) -> dict[str, list[int]]:
    """Intent ids per article uuid — every requested id gets an entry.

    Two callers with opposite deadlines share this: the TTL migration reads it
    BEFORE its cascade delete (the rows are gone right after, and the archived
    payload is the only place the history survives), while the search endpoint
    reads it AFTER retrieval to fill in ``matched_intents`` for hits that are
    still in ``news_current`` and therefore carry no such payload key.

    Read-only and chunked to stay under the bind-parameter cap.
    """
    result: dict[str, list[int]] = {a: [] for a in article_ids}
    for i in range(0, len(article_ids), _READ_CHUNK):
        chunk = article_ids[i : i + _READ_CHUNK]
        ph = ",".join("?" * len(chunk))
        async with conn.execute(
            f"SELECT article_id, intent_id FROM match_seen WHERE article_id IN ({ph})",
            chunk,
        ) as cur:
            for article_id, intent_id in await cur.fetchall():
                result[article_id].append(intent_id)
    return result

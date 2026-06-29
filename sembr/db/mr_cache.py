# SPDX-License-Identifier: Apache-2.0
"""mr_extraction_cache table: per-(article, intent, schema) structured extractions.

The map step extracts each cited article into a structured JSON record and stores
it here so the UI (and a later reduce step) read from SQLite, never re-calling the
LLM. Composite PK ``(article_id, intent_id, schema_version)``:

- ``article_id`` is the Qdrant point UUID of the cited article.
- ``intent_id`` scopes the extraction to the intent whose spec drove it.
- ``schema_version`` is computed by ``spec.load_spec`` as a short hash over the
  extraction prompt, the spec's *semantic projection* (field names/types/enums —
  not the display-only ``role``/``label``), and the extractor prompt version.
  Editing the semantics (or bumping the extractor) changes the version so a stale
  extraction is never served against a new spec; a pure ``role``/``label`` edit
  deliberately does not (it cannot change what was extracted).

Override re-extraction uses ``INSERT OR REPLACE``: the row is replaced and
``created_at`` re-defaults to now (so the UI can show the refreshed timestamp).
``FOREIGN KEY(intent_id) → intents(id) ON DELETE CASCADE`` means deleting an
intent drops its cached extractions automatically. Nothing here writes to Qdrant.
"""

from __future__ import annotations

import json

import aiosqlite

from sembr.db.sqlite import transaction

_CREATE_MR_CACHE = """
CREATE TABLE IF NOT EXISTS mr_extraction_cache (
    article_id      TEXT    NOT NULL,
    intent_id       INTEGER NOT NULL,
    schema_version  TEXT    NOT NULL,
    extraction      TEXT    NOT NULL,
    title           TEXT,
    source_name     TEXT,
    published_at    TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (article_id, intent_id, schema_version),
    FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE
)
"""

# The PK leads with article_id, so the digest-level "how many of this intent's
# articles are cached at the current spec version" query (intent_id + version)
# would full-scan without this covering index.
_CREATE_IDX_MR_CACHE_INTENT = (
    "CREATE INDEX IF NOT EXISTS idx_mr_cache_intent_version "
    "ON mr_extraction_cache(intent_id, schema_version)"
)


async def init_mr_cache_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_MR_CACHE)
    await conn.execute(_CREATE_IDX_MR_CACHE_INTENT)
    await conn.commit()


def _row_to_dict(row: tuple) -> dict:
    # cols: article_id, intent_id, schema_version, extraction, title, source_name,
    #       published_at, created_at
    return {
        "article_id": row[0],
        "intent_id": row[1],
        "schema_version": row[2],
        "extraction": json.loads(row[3]),
        "title": row[4],
        "source_name": row[5],
        "published_at": row[6],
        "created_at": row[7],
    }


async def put_extraction(
    conn: aiosqlite.Connection,
    *,
    article_id: str,
    intent_id: int,
    schema_version: str,
    extraction: dict,
    title: str | None = None,
    source_name: str | None = None,
    published_at: str | None = None,
) -> None:
    """Upsert one extraction. INSERT OR REPLACE so override re-runs overwrite cleanly.

    ``created_at`` is intentionally omitted from the column list so its DEFAULT
    fires on every replace — the UI shows the latest extraction time after an
    override.

    ``conn`` is accepted for signature symmetry with the other db helpers; the
    write itself goes through ``transaction()``'s shared connection (the global
    write lock serialises it under the extract fan-out), not ``conn`` directly.
    """
    async with transaction() as txn:
        await txn.execute(
            """INSERT OR REPLACE INTO mr_extraction_cache
                   (article_id, intent_id, schema_version, extraction,
                    title, source_name, published_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                article_id,
                intent_id,
                schema_version,
                json.dumps(extraction, ensure_ascii=False),
                title,
                source_name,
                published_at,
            ),
        )


async def get_extraction(
    conn: aiosqlite.Connection,
    article_id: str,
    intent_id: int,
    schema_version: str,
) -> dict | None:
    """Return the cached extraction dict for the full PK, or None when absent."""
    async with conn.execute(
        "SELECT article_id, intent_id, schema_version, extraction, title, "
        "source_name, published_at, created_at FROM mr_extraction_cache "
        "WHERE article_id=? AND intent_id=? AND schema_version=?",
        (article_id, intent_id, schema_version),
    ) as cur:
        row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def extraction_exists(
    conn: aiosqlite.Connection,
    article_id: str,
    intent_id: int,
    schema_version: str,
) -> bool:
    """Cheap PK existence check — drives the skip-on-cache-hit path when not override."""
    async with conn.execute(
        "SELECT 1 FROM mr_extraction_cache "
        "WHERE article_id=? AND intent_id=? AND schema_version=? LIMIT 1",
        (article_id, intent_id, schema_version),
    ) as cur:
        return await cur.fetchone() is not None

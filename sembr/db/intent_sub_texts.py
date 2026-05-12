"""intent_sub_texts table: auxiliary intent texts for cross-language match recall.

Mirrors db/match_seen.py's child-table-with-ON-DELETE-CASCADE pattern:
  - PK (intent_id, slot) makes positional slot identity explicit at the DB level.
  - CASCADE wipes sub_texts on intent delete; no manual cleanup in delete_intent.
  - slot CHECK (0,1,2) hard-caps at 3; Pydantic max_length=3 also enforces (defense in depth).
"""

from __future__ import annotations

import aiosqlite

from sembr.db.sqlite import transaction
from sembr.models import SubTextSpec

_CREATE_INTENT_SUB_TEXTS = """
CREATE TABLE IF NOT EXISTS intent_sub_texts (
    intent_id INTEGER NOT NULL,
    slot      INTEGER NOT NULL CHECK (slot IN (0,1,2)),
    language  TEXT    NOT NULL DEFAULT '',
    text      TEXT    NOT NULL,
    PRIMARY KEY (intent_id, slot),
    FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE
)
"""


async def init_intent_sub_texts_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_INTENT_SUB_TEXTS)
    await conn.commit()


async def list_for_intent(conn: aiosqlite.Connection, intent_id: int) -> list[SubTextSpec]:
    """Return sub_texts ordered by slot (slot=index for caller)."""
    async with conn.execute(
        "SELECT language, text FROM intent_sub_texts WHERE intent_id=? ORDER BY slot ASC",
        (intent_id,),
    ) as cur:
        rows = await cur.fetchall()
    return [SubTextSpec(language=r[0], text=r[1]) for r in rows]


async def _replace_in_txn(
    txn: aiosqlite.Connection,
    intent_id: int,
    sub_texts: list[SubTextSpec],
) -> None:
    """DELETE all + INSERT new inside an existing transaction.

    Caller is responsible for opening/committing the transaction. Used by
    create_intent to keep INSERT intents + INSERT sub_texts atomic.
    """
    await txn.execute("DELETE FROM intent_sub_texts WHERE intent_id=?", (intent_id,))
    if not sub_texts:
        return
    rows = [(intent_id, slot, st.language, st.text) for slot, st in enumerate(sub_texts)]
    await txn.executemany(
        "INSERT INTO intent_sub_texts (intent_id, slot, language, text) VALUES (?,?,?,?)",
        rows,
    )


async def replace_for_intent(
    conn: aiosqlite.Connection,
    intent_id: int,
    sub_texts: list[SubTextSpec],
) -> None:
    """Full-list replace: atomic DELETE+INSERT per intent_id."""
    async with transaction() as txn:
        await _replace_in_txn(txn, intent_id, sub_texts)


async def clear_intent_sub_texts(conn: aiosqlite.Connection, intent_id: int) -> None:
    """Used by POST rollback. DELETE intents handles this via CASCADE on the
    normal path, so this helper exists for the rare partial-failure path where
    sub_texts were written but the intents row needs an explicit rollback step.
    """
    async with transaction() as txn:
        await txn.execute("DELETE FROM intent_sub_texts WHERE intent_id=?", (intent_id,))

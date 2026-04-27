"""Intent persistence — intents table CRUD.

DDL is idempotent (CREATE TABLE IF NOT EXISTS). All functions accept the
global aiosqlite connection from get_conn() so callers don't open their own.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import aiosqlite

from sembr.models import Intent, IntentChannel, IntentCreate, IntentUpdate

_CREATE_INTENTS = """
CREATE TABLE IF NOT EXISTS intents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    text        TEXT NOT NULL,
    threshold   REAL NOT NULL DEFAULT 0.75,
    enabled     INTEGER NOT NULL DEFAULT 1,
    channels    TEXT NOT NULL DEFAULT '[]',
    tags        TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
)
"""

_SELECT_INTENTS = (
    "SELECT id,name,text,threshold,enabled,channels,tags,created_at,updated_at FROM intents"
)


async def init_intent_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_INTENTS)
    await conn.commit()


def _row_to_intent(row: tuple) -> Intent:
    return Intent(
        id=row[0],
        name=row[1],
        text=row[2],
        threshold=row[3],
        enabled=bool(row[4]),
        channels=[IntentChannel(**c) for c in json.loads(row[5])],
        tags=json.loads(row[6]),
        created_at=row[7],
        updated_at=row[8],
    )


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def create_intent(conn: aiosqlite.Connection, body: IntentCreate) -> Intent:
    channels_json = json.dumps([c.model_dump() for c in body.channels], ensure_ascii=False)
    tags_json = json.dumps(body.tags, ensure_ascii=False)
    now = _now_utc()
    cursor = await conn.execute(
        """INSERT INTO intents (name,text,threshold,enabled,channels,tags,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (body.name, body.text, body.threshold, int(body.enabled), channels_json, tags_json, now, now),
    )
    await conn.commit()
    assert cursor.lastrowid is not None  # AUTOINCREMENT INSERT on SQLite always sets lastrowid (M2)
    result = await get_intent(conn, cursor.lastrowid)
    return result  # type: ignore[return-value]


async def list_intents(conn: aiosqlite.Connection) -> list[Intent]:
    async with conn.execute(_SELECT_INTENTS) as cur:
        rows = await cur.fetchall()
    return [_row_to_intent(r) for r in rows]


async def get_intent(conn: aiosqlite.Connection, intent_id: int) -> Intent | None:
    async with conn.execute(_SELECT_INTENTS + " WHERE id=?", (intent_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_intent(row) if row else None


async def update_intent(conn: aiosqlite.Connection, intent_id: int, body: IntentUpdate) -> Intent:
    current = await get_intent(conn, intent_id)
    if current is None:  # explicit raise instead of assert — not stripped under python -O (I6)
        raise ValueError(f"intent {intent_id} not found; caller must check existence first")

    new_name = body.name if body.name is not None else current.name
    new_text = body.text if body.text is not None else current.text
    new_threshold = body.threshold if body.threshold is not None else current.threshold
    new_enabled = body.enabled if body.enabled is not None else current.enabled
    new_channels = body.channels if body.channels is not None else current.channels
    new_tags = body.tags if body.tags is not None else current.tags

    channels_json = json.dumps([c.model_dump() for c in new_channels], ensure_ascii=False)
    tags_json = json.dumps(new_tags, ensure_ascii=False)
    now = _now_utc()
    await conn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,updated_at=?
           WHERE id=?""",
        (new_name, new_text, new_threshold, int(new_enabled), channels_json, tags_json, now, intent_id),
    )
    await conn.commit()
    result = await get_intent(conn, intent_id)
    return result  # type: ignore[return-value]


async def update_intent_raw(conn: aiosqlite.Connection, intent_id: int, snapshot: Intent) -> None:
    """Restore a snapshot to roll back a failed PUT (R7: write original updated_at, not now)."""
    channels_json = json.dumps([c.model_dump() for c in snapshot.channels], ensure_ascii=False)
    tags_json = json.dumps(snapshot.tags, ensure_ascii=False)
    await conn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,updated_at=?
           WHERE id=?""",
        (
            snapshot.name,
            snapshot.text,
            snapshot.threshold,
            int(snapshot.enabled),
            channels_json,
            tags_json,
            snapshot.updated_at,
            intent_id,
        ),
    )
    await conn.commit()


async def delete_intent(conn: aiosqlite.Connection, intent_id: int) -> bool:
    cursor = await conn.execute("DELETE FROM intents WHERE id=?", (intent_id,))
    await conn.commit()
    return cursor.rowcount > 0

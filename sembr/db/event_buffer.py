"""Event buffer persistence — event_pending DDL.

Low-level DB helpers for the event-driven intent matching path.
Business logic (absorb / flush) lives in sembr/matcher/event_buffer.py.
"""
from __future__ import annotations

import sqlite3

import aiosqlite

_CREATE_EVENT_PENDING = """
CREATE TABLE IF NOT EXISTS event_pending (
    intent_id        INTEGER NOT NULL,
    group_id         INTEGER NOT NULL,
    rep_article_id   TEXT    NOT NULL,
    rep_title_norm   TEXT    NOT NULL,
    members_json     TEXT    NOT NULL,
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (intent_id, group_id),
    FOREIGN KEY (intent_id) REFERENCES intents(id) ON DELETE CASCADE
)
"""

_MIGRATIONS_EVENT: list[str] = []


async def init_event_buffer_tables(conn: aiosqlite.Connection) -> None:
    # PRAGMA foreign_keys=ON is set globally in sembr.db.sqlite.init_sqlite.
    await conn.execute(_CREATE_EVENT_PENDING)
    for migration in _MIGRATIONS_EVENT:
        try:
            await conn.execute(migration)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "no such column" in msg:
                pass
            else:
                raise
    await conn.commit()

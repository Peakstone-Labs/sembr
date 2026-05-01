"""Intent persistence — intents table CRUD.

DDL is idempotent (CREATE TABLE IF NOT EXISTS). All functions accept the
global aiosqlite connection from get_conn() so callers don't open their own.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import aiosqlite
from pydantic import TypeAdapter

from sembr.models import ChannelConfig, FeedFilter, Intent, IntentCreate, IntentUpdate, Schedule

# Reused per-call: cheaper than re-building the validator each time a row is parsed.
_CHANNEL_ADAPTER: TypeAdapter[ChannelConfig] = TypeAdapter(ChannelConfig)
_SCHEDULE_ADAPTER: TypeAdapter[Schedule] = TypeAdapter(Schedule)

_CREATE_INTENTS = """
CREATE TABLE IF NOT EXISTS intents (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT    NOT NULL,
    text                    TEXT    NOT NULL,
    threshold               REAL    NOT NULL DEFAULT 0.75,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    channels                TEXT    NOT NULL DEFAULT '[]',
    tags                    TEXT    NOT NULL DEFAULT '[]',
    scan_interval_seconds   INTEGER NOT NULL DEFAULT 3600,
    lookback_window_seconds INTEGER NOT NULL DEFAULT 86400,
    first_scan_at           TEXT,
    created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

# Migrations for databases created before the reverse-rag feature was added.
# ALTER TABLE ADD COLUMN is idempotent via exception suppression.
_MIGRATIONS = [
    "ALTER TABLE intents ADD COLUMN scan_interval_seconds INTEGER NOT NULL DEFAULT 3600",
    "ALTER TABLE intents ADD COLUMN lookback_window_seconds INTEGER NOT NULL DEFAULT 86400",
    "ALTER TABLE intents ADD COLUMN first_scan_at TEXT",
    "ALTER TABLE intents ADD COLUMN custom_prompt TEXT",
    "ALTER TABLE intents ADD COLUMN skip_seen INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE intents ADD COLUMN feed_filter TEXT NOT NULL DEFAULT 'null'",
    "ALTER TABLE intents ADD COLUMN schedule TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE intents ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'",
    "ALTER TABLE intents ADD COLUMN language TEXT NOT NULL DEFAULT 'zh'",
]

# Backfill old scan_interval_seconds into the new schedule JSON column.
# WHERE schedule='{}' makes this idempotent — only runs on rows not yet migrated.
_DATA_BACKFILL = [
    """UPDATE intents SET schedule = json_object('mode','interval','seconds',
                                     COALESCE(scan_interval_seconds, 3600))
       WHERE schedule = '{}' OR schedule IS NULL""",
]

# Drop legacy column after backfill. try/except in init_intent_tables suppresses
# "no such column" when the column was never added (fresh DBs or already dropped).
_DROP_COLUMNS = [
    "ALTER TABLE intents DROP COLUMN scan_interval_seconds",
]

_SELECT_INTENTS = (
    "SELECT id,name,text,threshold,enabled,channels,tags,"
    "lookback_window_seconds,first_scan_at,custom_prompt,"
    "skip_seen,feed_filter,schedule,timezone,language,"
    "created_at,updated_at FROM intents"
)


async def init_intent_tables(conn: aiosqlite.Connection) -> None:
    await conn.execute(_CREATE_INTENTS)
    for migration in _MIGRATIONS:
        try:
            await conn.execute(migration)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "duplicate column" in msg or "no such column" in msg:
                pass  # column already exists — expected on databases created with new schema
            else:
                raise
    for backfill in _DATA_BACKFILL:
        try:
            await conn.execute(backfill)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" in msg:
                pass  # scan_interval_seconds column absent on fresh DBs — no rows to backfill
            else:
                raise
    for drop in _DROP_COLUMNS:
        try:
            await conn.execute(drop)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" in msg:
                pass  # column already dropped or never existed
            else:
                raise
    await conn.commit()


def _parse_dt(s: str | None) -> datetime | None:
    if s is None:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _parse_feed_filter(raw: str | None) -> FeedFilter | None:
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        return None
    return FeedFilter.model_validate(data)


def _row_to_intent(row: tuple) -> Intent:
    # row indices: 0=id 1=name 2=text 3=threshold 4=enabled 5=channels 6=tags
    #              7=lookback_window_seconds 8=first_scan_at 9=custom_prompt
    #              10=skip_seen 11=feed_filter 12=schedule 13=timezone 14=language
    #              15=created_at 16=updated_at
    schedule = _SCHEDULE_ADAPTER.validate_python(json.loads(row[12]))
    return Intent(
        id=row[0],
        name=row[1],
        text=row[2],
        threshold=row[3],
        enabled=bool(row[4]),
        channels=[_CHANNEL_ADAPTER.validate_python(c) for c in json.loads(row[5])],
        tags=json.loads(row[6]),
        lookback_window_seconds=row[7],
        first_scan_at=_parse_dt(row[8]),
        custom_prompt=row[9],
        skip_seen=bool(row[10]),
        feed_filter=_parse_feed_filter(row[11]),
        schedule=schedule,
        timezone=row[13],
        language=row[14],
        created_at=row[15],
        updated_at=row[16],
    )


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed_filter_json(ff: FeedFilter | None) -> str:
    return ff.model_dump_json() if ff is not None else "null"


async def create_intent(conn: aiosqlite.Connection, body: IntentCreate) -> Intent:
    channels_json = json.dumps([c.model_dump() for c in body.channels], ensure_ascii=False)
    tags_json = json.dumps(body.tags, ensure_ascii=False)
    first_scan_at_str = body.first_scan_at.isoformat() if body.first_scan_at else None
    schedule_json = body.schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(body.feed_filter)
    now = _now_utc()
    cursor = await conn.execute(
        """INSERT INTO intents
               (name,text,threshold,enabled,channels,tags,
                lookback_window_seconds,first_scan_at,custom_prompt,
                skip_seen,feed_filter,schedule,timezone,language,
                created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.name,
            body.text,
            body.threshold,
            int(body.enabled),
            channels_json,
            tags_json,
            body.lookback_window_seconds,
            first_scan_at_str,
            body.custom_prompt,
            int(body.skip_seen),
            feed_filter_json,
            schedule_json,
            body.timezone,
            body.language,
            now,
            now,
        ),
    )
    await conn.commit()
    assert cursor.lastrowid is not None  # AUTOINCREMENT INSERT on SQLite always sets lastrowid (M2)
    result = await get_intent(conn, cursor.lastrowid)
    return result  # type: ignore[return-value]


async def list_intents(
    conn: aiosqlite.Connection,
    enabled: bool | None = None,
) -> list[Intent]:
    if enabled is None:
        sql, params = _SELECT_INTENTS, ()
    else:
        sql, params = _SELECT_INTENTS + " WHERE enabled=?", (int(enabled),)
    async with conn.execute(sql, params) as cur:
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
    new_schedule = body.schedule if body.schedule is not None else current.schedule
    new_lookback = (
        body.lookback_window_seconds if body.lookback_window_seconds is not None
        else current.lookback_window_seconds
    )
    # None in IntentUpdate means "no change"; first_scan_at can only be set, not cleared via PUT
    new_first_scan_at = body.first_scan_at if body.first_scan_at is not None else current.first_scan_at
    first_scan_at_str = new_first_scan_at.isoformat() if new_first_scan_at else None
    # custom_prompt=None in IntentUpdate means "no change"; explicit empty string clears it
    new_custom_prompt = body.custom_prompt if body.custom_prompt is not None else current.custom_prompt
    new_skip_seen = body.skip_seen if body.skip_seen is not None else current.skip_seen
    # Use model_fields_set to distinguish explicit null (clear to full-scan) from omitted (no-op)
    new_feed_filter = body.feed_filter if "feed_filter" in body.model_fields_set else current.feed_filter
    new_timezone = body.timezone if body.timezone is not None else current.timezone
    new_language = body.language if body.language is not None else current.language

    channels_json = json.dumps([c.model_dump() for c in new_channels], ensure_ascii=False)
    tags_json = json.dumps(new_tags, ensure_ascii=False)
    schedule_json = new_schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(new_feed_filter)
    now = _now_utc()
    await conn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,
               lookback_window_seconds=?,first_scan_at=?,custom_prompt=?,
               skip_seen=?,feed_filter=?,schedule=?,timezone=?,language=?,
               updated_at=?
           WHERE id=?""",
        (
            new_name,
            new_text,
            new_threshold,
            int(new_enabled),
            channels_json,
            tags_json,
            new_lookback,
            first_scan_at_str,
            new_custom_prompt,
            int(new_skip_seen),
            feed_filter_json,
            schedule_json,
            new_timezone,
            new_language,
            now,
            intent_id,
        ),
    )
    await conn.commit()
    result = await get_intent(conn, intent_id)
    return result  # type: ignore[return-value]


async def update_intent_raw(conn: aiosqlite.Connection, intent_id: int, snapshot: Intent) -> None:
    """Restore a snapshot to roll back a failed PUT (R7: write original updated_at, not now)."""
    channels_json = json.dumps([c.model_dump() for c in snapshot.channels], ensure_ascii=False)
    tags_json = json.dumps(snapshot.tags, ensure_ascii=False)
    first_scan_at_str = snapshot.first_scan_at.isoformat() if snapshot.first_scan_at else None
    schedule_json = snapshot.schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(snapshot.feed_filter)
    await conn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,
               lookback_window_seconds=?,first_scan_at=?,custom_prompt=?,
               skip_seen=?,feed_filter=?,schedule=?,timezone=?,language=?,
               updated_at=?
           WHERE id=?""",
        (
            snapshot.name,
            snapshot.text,
            snapshot.threshold,
            int(snapshot.enabled),
            channels_json,
            tags_json,
            snapshot.lookback_window_seconds,
            first_scan_at_str,
            snapshot.custom_prompt,
            int(snapshot.skip_seen),
            feed_filter_json,
            schedule_json,
            snapshot.timezone,
            snapshot.language,
            snapshot.updated_at,
            intent_id,
        ),
    )
    await conn.commit()


async def delete_intent(conn: aiosqlite.Connection, intent_id: int) -> bool:
    cursor = await conn.execute("DELETE FROM intents WHERE id=?", (intent_id,))
    await conn.commit()
    return cursor.rowcount > 0


async def intents_remove_feed_id(conn: aiosqlite.Connection, feed_id: int) -> list[int]:
    """Remove feed_id from all intent feed_filter.ids; return affected intent IDs.

    Does NOT commit — callers must commit (or rollback) the transaction.
    Designed to be called together with delete_feed() in the same implicit
    transaction so both changes land atomically.
    """
    async with conn.execute(
        """UPDATE intents
              SET feed_filter = CASE
                    WHEN json_extract(feed_filter, '$.ids') IS NULL THEN feed_filter
                    WHEN json_array_length(json_extract(feed_filter, '$.ids')) = 0 THEN feed_filter
                    ELSE json_set(feed_filter, '$.ids',
                           (SELECT json_group_array(value)
                              FROM json_each(json_extract(feed_filter, '$.ids'))
                             WHERE value != ?))
                  END,
                  updated_at = ?
            WHERE json_extract(feed_filter, '$.ids') IS NOT NULL
              AND EXISTS (SELECT 1 FROM json_each(json_extract(feed_filter, '$.ids')) WHERE value = ?)
            RETURNING id""",
        (feed_id, _now_utc(), feed_id),
    ) as cur:
        rows = await cur.fetchall()
    return [r[0] for r in rows]

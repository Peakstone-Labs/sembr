# SPDX-License-Identifier: Apache-2.0
"""Intent persistence — intents table CRUD.

DDL is idempotent (CREATE TABLE IF NOT EXISTS). All functions accept the
global aiosqlite connection from get_conn() so callers don't open their own.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime

import aiosqlite
from pydantic import TypeAdapter

logger = logging.getLogger(__name__)

from sembr.db.intent_sub_texts import (
    _replace_in_txn as _sub_texts_replace_in_txn,
)
from sembr.db.intent_sub_texts import (
    init_intent_sub_texts_tables,
)
from sembr.db.intent_sub_texts import (
    list_for_intent as _list_sub_texts_for_intent,
)
from sembr.db.sqlite import transaction
from sembr.models import ChannelConfig, FeedFilter, Intent, IntentCreate, IntentUpdate, Schedule

# Reused per-call: cheaper than re-building the validator each time a row is parsed.
_CHANNEL_ADAPTER: TypeAdapter[ChannelConfig] = TypeAdapter(ChannelConfig)
_SCHEDULE_ADAPTER: TypeAdapter[Schedule] = TypeAdapter(Schedule)

_CREATE_INTENTS = """
CREATE TABLE IF NOT EXISTS intents (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT    NOT NULL,
    text                 TEXT    NOT NULL,
    threshold            REAL    NOT NULL DEFAULT 0.75,
    enabled              INTEGER NOT NULL DEFAULT 1,
    channels             TEXT    NOT NULL DEFAULT '[]',
    tags                 TEXT    NOT NULL DEFAULT '[]',
    system_template      TEXT    NOT NULL DEFAULT 'default',
    instruction_template TEXT    NOT NULL DEFAULT 'default',
    feed_filter          TEXT    NOT NULL DEFAULT 'null',
    schedule             TEXT    NOT NULL DEFAULT '{}',
    timezone             TEXT    NOT NULL DEFAULT 'UTC',
    language             TEXT    NOT NULL DEFAULT 'zh',
    review_gate          INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""

# Migrations for databases created before the reverse-rag feature was added.
# ALTER TABLE ADD COLUMN is idempotent via exception suppression.
#
# NOTE: entries 0..4 below (scan_interval_seconds..skip_seen) add columns that
# _DROP_COLUMNS removes in the same init pass. They exist solely to upgrade
# pre-event-driven-intent DBs that still have those columns with live data.
# On a fresh DB they add-then-drop immediately (wasted I/O, but idempotent).
# Do NOT add new schema additions here; they go into _CREATE_INTENTS directly.
_MIGRATIONS = [
    "ALTER TABLE intents ADD COLUMN scan_interval_seconds INTEGER NOT NULL DEFAULT 3600",
    "ALTER TABLE intents ADD COLUMN lookback_window_seconds INTEGER NOT NULL DEFAULT 86400",
    "ALTER TABLE intents ADD COLUMN first_scan_at TEXT",
    "ALTER TABLE intents ADD COLUMN custom_prompt TEXT",
    "ALTER TABLE intents ADD COLUMN skip_seen INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE intents ADD COLUMN system_template TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE intents ADD COLUMN instruction_template TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE intents ADD COLUMN feed_filter TEXT NOT NULL DEFAULT 'null'",
    "ALTER TABLE intents ADD COLUMN schedule TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE intents ADD COLUMN timezone TEXT NOT NULL DEFAULT 'UTC'",
    "ALTER TABLE intents ADD COLUMN language TEXT NOT NULL DEFAULT 'zh'",
    "ALTER TABLE intents ADD COLUMN review_gate INTEGER NOT NULL DEFAULT 0",
]

# Backfill old scan_interval_seconds into the new schedule JSON column.
# WHERE schedule='{}' makes this idempotent — only runs on rows not yet migrated.
_DATA_BACKFILL = [
    """UPDATE intents SET schedule = json_object('mode','interval','seconds',
                                     COALESCE(scan_interval_seconds, 3600))
       WHERE schedule = '{}' OR schedule IS NULL""",
]

# Convert interval-mode rows to cron and sink lookback/skip_seen into schedule JSON.
# All statements are idempotent via WHERE conditions.
_DATA_MIGRATIONS = [
    # Translate interval schedule → cron preset; absorb top-level lookback/skip_seen
    """UPDATE intents SET schedule = json_object(
           'mode', 'cron',
           'preset', CASE
               WHEN json_extract(schedule,'$.seconds') <= 3600 THEN 'hourly'
               WHEN json_extract(schedule,'$.seconds') <= 86400 THEN 'daily'
               ELSE 'weekly'
           END,
           'hour', CASE
               WHEN json_extract(schedule,'$.seconds') <= 3600 THEN 0
               ELSE 9
           END,
           'minute', 0,
           'weekday', CASE
               WHEN json_extract(schedule,'$.seconds') > 86400 THEN 'mon'
               ELSE NULL
           END,
           'lookback_seconds', COALESCE(lookback_window_seconds, 86400),
           'skip_seen', COALESCE(skip_seen, 1) = 1
       )
       WHERE json_extract(schedule,'$.mode') = 'interval'""",
    # Sink lookback_seconds / skip_seen into existing cron rows that lack them
    """UPDATE intents SET schedule = json_set(
           schedule,
           '$.lookback_seconds', COALESCE(
               json_extract(schedule,'$.lookback_seconds'), lookback_window_seconds, 86400),
           '$.skip_seen', COALESCE(
               json_extract(schedule,'$.skip_seen') = 1, skip_seen = 1, 1) = 1
       )
       WHERE json_extract(schedule,'$.mode') = 'cron'
         AND (json_extract(schedule,'$.lookback_seconds') IS NULL
              OR json_extract(schedule,'$.skip_seen') IS NULL)""",
]

# Drop legacy columns after data migrations. try/except in init_intent_tables suppresses
# "no such column" when the column was never added (fresh DBs or already dropped).
_DROP_COLUMNS = [
    "ALTER TABLE intents DROP COLUMN scan_interval_seconds",
    "ALTER TABLE intents DROP COLUMN custom_prompt",
    "ALTER TABLE intents DROP COLUMN lookback_window_seconds",
    "ALTER TABLE intents DROP COLUMN first_scan_at",
    "ALTER TABLE intents DROP COLUMN skip_seen",
]

_SELECT_INTENTS = (
    "SELECT id,name,text,threshold,enabled,channels,tags,"
    "system_template,instruction_template,"
    "feed_filter,schedule,timezone,language,"
    "review_gate,"
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
    for migration in _DATA_MIGRATIONS:
        try:
            await conn.execute(migration)
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "no such column" in msg:
                pass  # legacy columns absent on fresh DBs — no rows to migrate
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
    # Sub-texts is conceptually an extension of the intents row (FK with CASCADE);
    # chaining the init avoids 40+ test-fixture updates that would otherwise need
    # to call both functions side-by-side. CREATE TABLE IF NOT EXISTS is idempotent.
    await init_intent_sub_texts_tables(conn)
    # Warn on startup if any rows have an unrecognized schedule mode; these would crash _row_to_intent
    async with conn.execute(
        "SELECT id, json_extract(schedule, '$.mode') FROM intents "
        "WHERE json_extract(schedule, '$.mode') NOT IN ('cron', 'event') "
        "   OR json_extract(schedule, '$.mode') IS NULL"
    ) as cur:
        bad_rows = await cur.fetchall()
    if bad_rows:
        logger.warning(
            "init_intent_tables: %d intent row(s) have unrecognized schedule mode: %s",
            len(bad_rows),
            [(row[0], row[1]) for row in bad_rows],
        )


def _parse_feed_filter(raw: str | None) -> FeedFilter | None:
    if not raw:
        return None
    data = json.loads(raw)
    if not isinstance(data, dict):
        return None
    return FeedFilter.model_validate(data)


def _row_to_intent(row: tuple) -> Intent:
    # row indices: 0=id 1=name 2=text 3=threshold 4=enabled 5=channels 6=tags
    #              7=system_template 8=instruction_template
    #              9=feed_filter 10=schedule 11=timezone 12=language
    #              13=review_gate 14=created_at 15=updated_at
    schedule = _SCHEDULE_ADAPTER.validate_python(json.loads(row[10]))
    return Intent(
        id=row[0],
        name=row[1],
        text=row[2],
        threshold=row[3],
        enabled=bool(row[4]),
        channels=[_CHANNEL_ADAPTER.validate_python(c) for c in json.loads(row[5])],
        tags=json.loads(row[6]),
        system_template=row[7],
        instruction_template=row[8],
        feed_filter=_parse_feed_filter(row[9]),
        schedule=schedule,
        timezone=row[11],
        language=row[12],
        review_gate=bool(row[13]),
        created_at=row[14],
        updated_at=row[15],
    )


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _feed_filter_json(ff: FeedFilter | None) -> str:
    return ff.model_dump_json() if ff is not None else "null"


async def create_intent(conn: aiosqlite.Connection, body: IntentCreate) -> Intent:
    channels_json = json.dumps([c.model_dump() for c in body.channels], ensure_ascii=False)
    tags_json = json.dumps(body.tags, ensure_ascii=False)
    schedule_json = body.schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(body.feed_filter)
    now = _now_utc()
    async with transaction() as txn:
        cursor = await txn.execute(
            """INSERT INTO intents
                   (name,text,threshold,enabled,channels,tags,
                    system_template,instruction_template,
                    feed_filter,schedule,timezone,language,
                    review_gate,
                    created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body.name,
                body.text,
                body.threshold,
                int(body.enabled),
                channels_json,
                tags_json,
                body.system_template,
                body.instruction_template,
                feed_filter_json,
                schedule_json,
                body.timezone,
                body.language,
                int(body.review_gate),
                now,
                now,
            ),
        )
        assert cursor.lastrowid is not None  # AUTOINCREMENT INSERT on SQLite always sets lastrowid
        # Same transaction so the intents row + its sub_texts commit atomically.
        await _sub_texts_replace_in_txn(txn, cursor.lastrowid, body.sub_texts)
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
    intents = [_row_to_intent(r) for r in rows]
    for intent in intents:
        intent.sub_texts = await _list_sub_texts_for_intent(conn, intent.id)
    return intents


async def get_intent(conn: aiosqlite.Connection, intent_id: int) -> Intent | None:
    async with conn.execute(_SELECT_INTENTS + " WHERE id=?", (intent_id,)) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    intent = _row_to_intent(row)
    intent.sub_texts = await _list_sub_texts_for_intent(conn, intent_id)
    return intent


async def _update_intent_in_txn(
    txn: aiosqlite.Connection,
    intent_id: int,
    body: IntentUpdate,
    current: Intent,
) -> None:
    """UPDATE intents row only, inside the caller's transaction.

    Extracted from update_intent so PUT can co-commit the intents row and
    intent_sub_texts table inside a single outer transaction (Loop 2 🔴-1):
    the previous two-COMMIT pattern left intents updated but sub_texts
    rollback-less if the child write raised.

    Caller is responsible for the surrounding transaction() and for re-reading
    via get_intent (this function returns nothing; the post-write state is
    not visible until the transaction commits).
    """
    new_name = body.name if body.name is not None else current.name
    new_text = body.text if body.text is not None else current.text
    new_threshold = body.threshold if body.threshold is not None else current.threshold
    new_enabled = body.enabled if body.enabled is not None else current.enabled
    new_channels = body.channels if body.channels is not None else current.channels
    new_tags = body.tags if body.tags is not None else current.tags
    new_schedule = body.schedule if body.schedule is not None else current.schedule
    new_system_template = (
        body.system_template if body.system_template is not None else current.system_template
    )
    new_instruction_template = (
        body.instruction_template
        if body.instruction_template is not None
        else current.instruction_template
    )
    # Use model_fields_set to distinguish explicit null (clear to full-scan) from omitted (no-op)
    new_feed_filter = (
        body.feed_filter if "feed_filter" in body.model_fields_set else current.feed_filter
    )
    new_timezone = body.timezone if body.timezone is not None else current.timezone
    new_language = body.language if body.language is not None else current.language
    new_review_gate = body.review_gate if body.review_gate is not None else current.review_gate

    channels_json = json.dumps([c.model_dump() for c in new_channels], ensure_ascii=False)
    tags_json = json.dumps(new_tags, ensure_ascii=False)
    schedule_json = new_schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(new_feed_filter)
    now = _now_utc()
    await txn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,
               system_template=?,instruction_template=?,
               feed_filter=?,schedule=?,timezone=?,language=?,
               review_gate=?,
               updated_at=?
           WHERE id=?""",
        (
            new_name,
            new_text,
            new_threshold,
            int(new_enabled),
            channels_json,
            tags_json,
            new_system_template,
            new_instruction_template,
            feed_filter_json,
            schedule_json,
            new_timezone,
            new_language,
            int(new_review_gate),
            now,
            intent_id,
        ),
    )


async def update_intent(conn: aiosqlite.Connection, intent_id: int, body: IntentUpdate) -> Intent:
    current = await get_intent(conn, intent_id)
    if current is None:  # explicit raise instead of assert — not stripped under python -O (I6)
        raise ValueError(f"intent {intent_id} not found; caller must check existence first")
    async with transaction() as txn:
        await _update_intent_in_txn(txn, intent_id, body, current)
    result = await get_intent(conn, intent_id)
    return result  # type: ignore[return-value]


async def _update_intent_raw_in_txn(
    txn: aiosqlite.Connection,
    intent_id: int,
    snapshot: Intent,
) -> None:
    """Restore a snapshot row inside the caller's transaction (PUT rollback path).

    Counterpart of `_update_intent_in_txn` for the failure branch. Pairing the
    SQL restore with `_sub_texts_replace_in_txn(txn, id, snapshot.sub_texts)`
    inside one transaction prevents the same split-brain that 🔴-1 fixed on
    the happy path.
    """
    channels_json = json.dumps([c.model_dump() for c in snapshot.channels], ensure_ascii=False)
    tags_json = json.dumps(snapshot.tags, ensure_ascii=False)
    schedule_json = snapshot.schedule.model_dump_json()
    feed_filter_json = _feed_filter_json(snapshot.feed_filter)
    await txn.execute(
        """UPDATE intents
           SET name=?,text=?,threshold=?,enabled=?,channels=?,tags=?,
               system_template=?,instruction_template=?,
               feed_filter=?,schedule=?,timezone=?,language=?,
               review_gate=?,
               updated_at=?
           WHERE id=?""",
        (
            snapshot.name,
            snapshot.text,
            snapshot.threshold,
            int(snapshot.enabled),
            channels_json,
            tags_json,
            snapshot.system_template,
            snapshot.instruction_template,
            feed_filter_json,
            schedule_json,
            snapshot.timezone,
            snapshot.language,
            int(snapshot.review_gate),
            snapshot.updated_at,
            intent_id,
        ),
    )


async def update_intent_raw(conn: aiosqlite.Connection, intent_id: int, snapshot: Intent) -> None:
    """Restore a snapshot to roll back a failed PUT — writes the original updated_at, not now().

    Standalone transaction wrapper kept for backward compat with callers that
    only need to restore the intents row (not the child sub_texts table).

    created_at is intentionally not written — no code path mutates it after INSERT.
    """
    async with transaction() as txn:
        await _update_intent_raw_in_txn(txn, intent_id, snapshot)


async def delete_intent(conn: aiosqlite.Connection, intent_id: int) -> bool:
    async with transaction() as txn:
        await txn.execute("DELETE FROM intents WHERE id=?", (intent_id,))
        async with txn.execute("SELECT changes()") as cur:
            n = (await cur.fetchone())[0]
    return n > 0


# Kinds whitelist for column-name interpolation in list_template_refs /
# rename_intent_template. Frozen — never widened from API input.
_TEMPLATE_KINDS: frozenset[str] = frozenset({"system", "instruction"})


async def list_template_refs(
    conn: aiosqlite.Connection,
) -> dict[tuple[str, str], list[tuple[int, str]]]:
    """Scan intents once, group `(intent_id, intent_name)` by `(kind, template_name)`.

    Returned dict has keys ``("system", name)`` / ``("instruction", name)`` and
    values ``[(id, name), ...]`` ordered by intent id ASC. Templates with zero
    referencing intents are absent from the dict — caller handles missing keys
    as empty lists.
    """
    out: dict[tuple[str, str], list[tuple[int, str]]] = {}
    async with conn.execute(
        "SELECT id, name, system_template, instruction_template FROM intents ORDER BY id ASC"
    ) as cur:
        async for row in cur:
            intent_id, intent_name, sys_tpl, inst_tpl = row
            out.setdefault(("system", sys_tpl), []).append((intent_id, intent_name))
            out.setdefault(("instruction", inst_tpl), []).append((intent_id, intent_name))
    return out


async def rename_intent_template(
    conn: aiosqlite.Connection,
    kind: str,
    old: str,
    new: str,
) -> int:
    """Cascade-rename `kind`_template column from *old* to *new*.

    Returns the rowcount of affected intents. The *kind* string is whitelisted
    against ``_TEMPLATE_KINDS`` to keep the column-name interpolation safe — the
    column name is built from a frozen set, never from API input directly.

    Caller is expected to wrap this call in ``db.sqlite.transaction()``.
    """
    if kind not in _TEMPLATE_KINDS:
        raise ValueError(
            f"invalid template kind {kind!r}; expected one of {sorted(_TEMPLATE_KINDS)}"
        )
    column = f"{kind}_template"
    async with conn.execute(
        f"UPDATE intents SET {column} = ?, updated_at = ? WHERE {column} = ?",
        (new, _now_utc(), old),
    ) as cur:
        return cur.rowcount


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

"""Unit tests for DB migration strategy (M1).

Verifies that init_intent_tables is idempotent and correctly migrates
old interval-mode rows into cron-mode schedule JSON, and sinks
lookback_seconds / skip_seen from top-level columns into schedule JSON.
"""
from __future__ import annotations

import asyncio
import json

import aiosqlite
import pytest

from sembr.db.intents import init_intent_tables, create_intent, get_intent
from sembr.models import IntentCreate


VALID_INTENT = IntentCreate(
    name="test",
    text="migration test",
    channels=[{"type": "email", "to": ["a@example.com"]}],
)


@pytest.mark.asyncio
async def test_fresh_db_migration_idempotent() -> None:
    """init_intent_tables can be called multiple times without error."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_intent_tables(conn)
        await init_intent_tables(conn)  # second call must not raise
        intent = await create_intent(conn, VALID_INTENT)
        fetched = await get_intent(conn, intent.id)
        assert fetched is not None
        assert fetched.schedule.mode == "cron"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_old_db_backfill_scan_interval_seconds() -> None:
    """Old interval rows get converted to cron mode with embedded lookback/skip_seen."""
    conn = await aiosqlite.connect(":memory:")
    try:
        # Simulate an old-schema DB: create table without new columns,
        # insert a row with scan_interval_seconds=86400, then run migration.
        await conn.execute("""
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
                custom_prompt           TEXT,
                created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        await conn.execute(
            """INSERT INTO intents (name, text, channels, scan_interval_seconds, lookback_window_seconds, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("old-intent", "old text", "[]", 86400, 43200),
        )
        await conn.commit()

        await init_intent_tables(conn)

        # Verify conversion: interval → cron with embedded lookback_seconds
        async with conn.execute("SELECT schedule FROM intents WHERE name='old-intent'") as cur:
            row = await cur.fetchone()
        assert row is not None
        sched = json.loads(row[0])
        assert sched["mode"] == "cron"
        assert sched["lookback_seconds"] == 43200
        assert sched["skip_seen"] in (0, 1, True, False)  # stored as int or bool

        # Verify scan_interval_seconds column was dropped
        async with conn.execute("PRAGMA table_info(intents)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "scan_interval_seconds" not in cols
        assert "lookback_window_seconds" not in cols
        assert "first_scan_at" not in cols
        assert "skip_seen" not in cols

        # Verify new columns exist
        for col in ("feed_filter", "schedule", "timezone", "language"):
            assert col in cols, f"expected column {col!r} to exist after migration"

    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_multiple_old_rows() -> None:
    """Multiple old interval rows all get converted to cron mode."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS intents (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                name                    TEXT    NOT NULL,
                text                    TEXT    NOT NULL DEFAULT '',
                threshold               REAL    NOT NULL DEFAULT 0.75,
                enabled                 INTEGER NOT NULL DEFAULT 1,
                channels                TEXT    NOT NULL DEFAULT '[]',
                tags                    TEXT    NOT NULL DEFAULT '[]',
                scan_interval_seconds   INTEGER NOT NULL DEFAULT 3600,
                lookback_window_seconds INTEGER NOT NULL DEFAULT 86400,
                first_scan_at           TEXT,
                custom_prompt           TEXT,
                created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
                updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        rows = [("i1", 3600), ("i2", 7200), ("i3", 600)]
        for name, interval in rows:
            await conn.execute(
                "INSERT INTO intents (name, channels, scan_interval_seconds, created_at, updated_at) "
                "VALUES (?, '[]', ?, datetime('now'), datetime('now'))",
                (name, interval),
            )
        await conn.commit()

        await init_intent_tables(conn)

        async with conn.execute("SELECT name, schedule FROM intents ORDER BY name") as cur:
            result = await cur.fetchall()

        assert len(result) == 3
        for name, raw_schedule in result:
            sched = json.loads(raw_schedule)
            assert sched["mode"] == "cron", f"intent {name} should have been converted to cron"
            assert "lookback_seconds" in sched, f"intent {name} missing lookback_seconds"

        # Verify no NULL schedule values remain
        async with conn.execute("SELECT count(*) FROM intents WHERE json_extract(schedule,'$.mode') IS NULL") as cur:
            row = await cur.fetchone()
        assert row[0] == 0

    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_new_intent_schedule_defaults() -> None:
    """Freshly created intents have correct schedule defaults."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_intent_tables(conn)
        intent = await create_intent(conn, VALID_INTENT)
        assert intent.schedule.mode == "cron"
        assert intent.schedule.preset == "daily"  # type: ignore[union-attr]
        assert intent.schedule.lookback_seconds == 86400  # type: ignore[union-attr]
        assert intent.schedule.skip_seen is True  # type: ignore[union-attr]
        assert intent.feed_filter is None
        assert intent.timezone == "UTC"
        assert intent.language == "zh"
    finally:
        await conn.close()

"""Unit tests for DD7: DB migration strategy (M1).

Verifies that init_intent_tables is idempotent and correctly migrates
old scan_interval_seconds rows into the new schedule JSON column.
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
        # Basic smoke: create and retrieve an intent
        intent = await create_intent(conn, VALID_INTENT)
        fetched = await get_intent(conn, intent.id)
        assert fetched is not None
        assert fetched.schedule.mode == "interval"
        assert fetched.schedule.seconds == 3600  # type: ignore[union-attr]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_old_db_backfill_scan_interval_seconds() -> None:
    """Old rows with scan_interval_seconds get schedule JSON backfilled on migration."""
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
            """INSERT INTO intents (name, text, channels, scan_interval_seconds, created_at, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'), datetime('now'))""",
            ("old-intent", "old text", "[]", 86400),
        )
        await conn.commit()

        # Run migration
        await init_intent_tables(conn)

        # Verify backfill: schedule must reflect old scan_interval_seconds
        async with conn.execute("SELECT schedule FROM intents WHERE name='old-intent'") as cur:
            row = await cur.fetchone()
        assert row is not None
        sched = json.loads(row[0])
        assert sched["mode"] == "interval"
        assert sched["seconds"] == 86400

        # Verify scan_interval_seconds column was dropped
        async with conn.execute("PRAGMA table_info(intents)") as cur:
            cols = {r[1] for r in await cur.fetchall()}
        assert "scan_interval_seconds" not in cols

        # Verify all new columns exist
        for col in ("skip_seen", "feed_filter", "schedule", "timezone", "language"):
            assert col in cols, f"expected column {col!r} to exist after migration"

    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_multiple_old_rows() -> None:
    """Multiple old rows all get their scan_interval_seconds migrated."""
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
            assert sched["mode"] == "interval", f"intent {name} has wrong mode"

        name_to_interval = {name: json.loads(raw)["seconds"] for name, raw in result}
        assert name_to_interval["i1"] == 3600
        assert name_to_interval["i2"] == 7200
        assert name_to_interval["i3"] == 600

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
        assert intent.schedule.mode == "interval"
        assert intent.schedule.seconds == 3600  # type: ignore[union-attr]
        assert intent.skip_seen is True
        assert intent.feed_filter is None
        assert intent.timezone == "UTC"
        assert intent.language == "zh"
    finally:
        await conn.close()

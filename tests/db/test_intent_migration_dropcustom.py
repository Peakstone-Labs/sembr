# SPDX-License-Identifier: Apache-2.0
"""Verify DB migration: custom_prompt column is dropped; new template columns exist with defaults."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import aiosqlite
import pytest

from sembr.db.intents import init_intent_tables


def _create_legacy_db(path: str) -> None:
    """Build a DB that looks like it was created before this feature (has custom_prompt)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE intents (
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
            skip_seen               INTEGER NOT NULL DEFAULT 1,
            feed_filter             TEXT    NOT NULL DEFAULT 'null',
            schedule                TEXT    NOT NULL DEFAULT '{}',
            timezone                TEXT    NOT NULL DEFAULT 'UTC',
            language                TEXT    NOT NULL DEFAULT 'zh',
            created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        """INSERT INTO intents (name, text, custom_prompt, schedule)
           VALUES ('legacy', 'track AI news', 'old custom prompt text',
                  '{"mode":"interval","seconds":3600}')"""
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_migration_drops_custom_prompt_adds_template_columns() -> None:
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    _create_legacy_db(db_path)

    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await init_intent_tables(conn)

        # custom_prompt column must be gone
        async with conn.execute("PRAGMA table_info(intents)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

    assert "custom_prompt" not in columns, "custom_prompt column should have been dropped"
    assert "system_template" in columns, "system_template column must exist"
    assert "instruction_template" in columns, "instruction_template column must exist"

    # Verify the legacy row now has default template values
    sync_conn = sqlite3.connect(db_path)
    row = sync_conn.execute(
        "SELECT system_template, instruction_template FROM intents WHERE name='legacy'"
    ).fetchone()
    sync_conn.close()

    assert row is not None
    assert row[0] == "default", f"system_template should be 'default', got {row[0]!r}"
    assert row[1] == "default", f"instruction_template should be 'default', got {row[1]!r}"

    Path(db_path).unlink(missing_ok=True)  # noqa: ASYNC240


@pytest.mark.asyncio
async def test_migration_idempotent_on_fresh_db() -> None:
    """Running init_intent_tables twice on a fresh DB should not raise."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    async with aiosqlite.connect(db_path) as conn:
        await init_intent_tables(conn)
        await init_intent_tables(conn)  # second call — all migrations are no-ops

        async with conn.execute("PRAGMA table_info(intents)") as cur:
            columns = {row[1] for row in await cur.fetchall()}

    assert "system_template" in columns
    assert "instruction_template" in columns
    assert "custom_prompt" not in columns

    Path(db_path).unlink(missing_ok=True)  # noqa: ASYNC240

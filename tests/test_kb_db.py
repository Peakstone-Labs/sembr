# SPDX-License-Identifier: Apache-2.0
"""DB-layer tests for the per-intent KB switch (delta-label/kb SF1, design §6.1).

`kb_enabled` mirrors `extraction_enabled`: a real new column added via idempotent
ALTER migration, defaulting to 0 so existing intents are untouched, appended last
in the SELECT so every prior row index is stable.
"""

from __future__ import annotations

import aiosqlite

from sembr.db.intents import (
    create_intent,
    get_intent,
    init_intent_tables,
    update_intent,
)
from sembr.db.sqlite import install_for_test
from sembr.models import IntentCreate, IntentUpdate


def _intent_create() -> IntentCreate:
    return IntentCreate.model_validate(
        {
            "name": "kb-test",
            "text": "track china macro policy events",
            "channels": [{"type": "email", "to": ["a@example.com"]}],
        }
    )


async def _fresh_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    install_for_test(conn)
    return conn


async def _has_column(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        return any(row[1] == column for row in await cur.fetchall())


async def test_intents_kb_enabled_default_zero() -> None:
    conn = await _fresh_conn()
    try:
        intent = await create_intent(conn, _intent_create())
        assert intent.kb_enabled is False
        # Persisted value round-trips through SELECT/_row_to_intent (row[16]).
        reread = await get_intent(conn, intent.id)
        assert reread is not None
        assert reread.kb_enabled is False
    finally:
        await conn.close()


async def test_intents_kb_enabled_toggle() -> None:
    conn = await _fresh_conn()
    try:
        intent = await create_intent(conn, _intent_create())
        updated = await update_intent(conn, intent.id, IntentUpdate(kb_enabled=True))
        assert updated.kb_enabled is True
        # Toggling kb_enabled must not disturb the sibling extraction switch.
        assert updated.extraction_enabled is False
        back = await update_intent(conn, intent.id, IntentUpdate(kb_enabled=False))
        assert back.kb_enabled is False
    finally:
        await conn.close()


async def test_intents_kb_enabled_update_noop_when_omitted() -> None:
    conn = await _fresh_conn()
    try:
        intent = await create_intent(conn, _intent_create())
        await update_intent(conn, intent.id, IntentUpdate(kb_enabled=True))
        # An update that omits kb_enabled (None) must leave it as-is, not reset to default.
        unchanged = await update_intent(conn, intent.id, IntentUpdate(name="renamed"))
        assert unchanged.kb_enabled is True
        assert unchanged.name == "renamed"
    finally:
        await conn.close()


async def test_intents_kb_enabled_migration_idempotent() -> None:
    """Re-running init must not raise (ALTER hits duplicate-column suppression)."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        # Second init exercises the duplicate-column suppression branch.
        await init_intent_tables(conn)
        assert await _has_column(conn, "intents", "kb_enabled")
        install_for_test(conn)
        intent = await create_intent(conn, _intent_create())
        assert intent.kb_enabled is False
    finally:
        await conn.close()


async def test_intents_kb_enabled_migration_adds_column_to_old_db() -> None:
    """Simulate a pre-KB DB (column dropped) → init re-adds it with default 0."""
    conn = await aiosqlite.connect(":memory:")
    try:
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await conn.execute("ALTER TABLE intents DROP COLUMN kb_enabled")
        await conn.commit()
        assert not await _has_column(conn, "intents", "kb_enabled")
        # Re-init = the migration path an existing prod DB takes on upgrade.
        await init_intent_tables(conn)
        assert await _has_column(conn, "intents", "kb_enabled")
        install_for_test(conn)
        intent = await create_intent(conn, _intent_create())
        assert intent.kb_enabled is False
    finally:
        await conn.close()

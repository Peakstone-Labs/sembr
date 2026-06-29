# SPDX-License-Identifier: Apache-2.0
"""T11 (design §6/§9): summary_history.reduce_mode — save/list/get round-trip,
migration idempotency, and legacy rows reading back as None.
"""

from __future__ import annotations

import aiosqlite
import pytest

from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.sqlite import install_for_test
from sembr.db.summary_history import (
    get_summary,
    init_summary_history_table,
    list_summaries,
    migrate_summary_history_add_reduce_mode,
    save_summary,
    save_summary_or_skip,
)
from sembr.models import IntentCreate
from sembr.summarizer.models import SummaryResult

_LEGACY_CREATE = """
CREATE TABLE summary_history (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    intent_id   INTEGER  NOT NULL REFERENCES intents(id) ON DELETE CASCADE,
    run_at      TEXT     NOT NULL,
    summary     TEXT     NOT NULL,
    citations   TEXT     NOT NULL DEFAULT '[]'
)
"""


async def _intent(conn) -> int:
    intent = await create_intent(
        conn, IntentCreate(name="i", text="t", channels=[{"type": "email", "to": ["a@b.com"]}])
    )
    return intent.id


@pytest.fixture
async def mem_conn():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


async def test_save_summary_persists_reduce_mode(mem_conn):
    await init_summary_history_table(mem_conn)
    iid = await _intent(mem_conn)
    result = SummaryResult(intent_id=iid, summary="s", reduce_mode="facts_fallback_raw")

    row_id = await save_summary(mem_conn, result, run_at="2026-06-01T00:00:00Z")

    got = await get_summary(mem_conn, iid, row_id)
    assert got["reduce_mode"] == "facts_fallback_raw"
    rows = await list_summaries(mem_conn, iid)
    assert rows[0]["reduce_mode"] == "facts_fallback_raw"


async def test_save_or_skip_persists_reduce_mode(mem_conn):
    """Backfill path (save_summary_or_skip) also carries the reduce_mode marker."""
    await init_summary_history_table(mem_conn)
    iid = await _intent(mem_conn)
    result = SummaryResult(intent_id=iid, summary="s", reduce_mode="facts_partial")

    inserted = await save_summary_or_skip(mem_conn, result, run_at="2026-06-02T00:00:00Z")

    assert inserted is True
    rows = await list_summaries(mem_conn, iid)
    assert rows[0]["reduce_mode"] == "facts_partial"


async def test_reduce_mode_none_default(mem_conn):
    """A result with reduce_mode=None (legacy/test construction) persists as None."""
    await init_summary_history_table(mem_conn)
    iid = await _intent(mem_conn)
    row_id = await save_summary(
        mem_conn, SummaryResult(intent_id=iid, summary="s"), run_at="2026-06-03T00:00:00Z"
    )
    got = await get_summary(mem_conn, iid, row_id)
    assert got["reduce_mode"] is None


async def test_migration_idempotent_on_fresh_table(mem_conn):
    """init creates the column; the migration re-run on it must not raise."""
    await init_summary_history_table(mem_conn)  # already has reduce_mode
    await migrate_summary_history_add_reduce_mode(mem_conn)  # must not raise
    await migrate_summary_history_add_reduce_mode(mem_conn)  # idempotent twice
    iid = await _intent(mem_conn)
    row_id = await save_summary(
        mem_conn,
        SummaryResult(intent_id=iid, summary="s", reduce_mode="facts"),
        run_at="2026-06-04T00:00:00Z",
    )
    assert (await get_summary(mem_conn, iid, row_id))["reduce_mode"] == "facts"


async def test_migration_adds_column_to_legacy_db(mem_conn):
    """Legacy summary_history (no reduce_mode) → migration adds it; old rows read None."""
    await mem_conn.execute(_LEGACY_CREATE)
    await mem_conn.commit()
    iid = await _intent(mem_conn)
    # insert a legacy row directly (pre-migration shape)
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (?,?,?,?)",
        (iid, "2026-05-01T00:00:00Z", "old summary", "[]"),
    )
    await mem_conn.commit()

    await migrate_summary_history_add_reduce_mode(mem_conn)

    rows = await list_summaries(mem_conn, iid)
    assert rows[0]["reduce_mode"] is None  # legacy row reads back None, no crash
    # and new writes carry the marker
    await save_summary(
        mem_conn,
        SummaryResult(intent_id=iid, summary="new", reduce_mode="facts"),
        run_at="2026-06-05T00:00:00Z",
    )
    rows = await list_summaries(mem_conn, iid)
    modes = {r["run_at"]: r["reduce_mode"] for r in rows}
    assert modes["2026-05-01T00:00:00Z"] is None
    assert modes["2026-06-05T00:00:00Z"] == "facts"

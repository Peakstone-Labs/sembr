# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr/db/summary_history.py — save_summary + format_history_text."""

from __future__ import annotations

import json
from datetime import UTC

import aiosqlite
import pytest

from sembr.db.sqlite import install_for_test
from sembr.db.summary_history import (
    format_history_text,
    init_summary_history_table,
    save_summary,
)
from sembr.summarizer.models import Citation, SummaryResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    intent_id: int = 1,
    summary: str = "Test summary",
    citations: list[Citation] | None = None,
) -> SummaryResult:
    if citations is None:
        citations = [
            Citation(
                article_id="a1",
                title="Article 1",
                url="https://example.com/1",
                source=10,
                published_at="2026-05-26T00:00:00Z",
                score=0.9,
            )
        ]
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        citations=citations,
        primary=citations[0] if citations else None,
        other_sources=citations[1:],
    )


@pytest.fixture()
async def mem_conn():
    """In-memory SQLite with intents + summary_history tables."""
    async with aiosqlite.connect(":memory:") as conn:
        install_for_test(conn)
        await conn.execute("PRAGMA foreign_keys=ON")
        # intents table (minimal — only id needed for FK)
        await conn.execute(
            "CREATE TABLE intents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
        )
        await conn.execute("INSERT INTO intents (id, name) VALUES (1, 'test-intent')")
        await conn.execute("INSERT INTO intents (id, name) VALUES (2, 'other-intent')")
        await conn.commit()
        await init_summary_history_table(conn)
        yield conn


# ---------------------------------------------------------------------------
# init_summary_history_table — idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_summary_history_table_idempotent(mem_conn) -> None:
    """Calling init twice must not raise (CREATE TABLE IF NOT EXISTS)."""
    await init_summary_history_table(mem_conn)  # second call
    async with mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='summary_history'"
    ) as cur:
        row = await cur.fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# save_summary — basic persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_history_save_and_query(mem_conn) -> None:
    """save_summary inserts one row with correct fields."""
    result = _result()
    row_id = await save_summary(mem_conn, result, run_at="2026-05-26T09:00:00Z")
    assert isinstance(row_id, int) and row_id > 0

    async with mem_conn.execute(
        "SELECT intent_id, run_at, summary, citations FROM summary_history WHERE id=?", (row_id,)
    ) as cur:
        row = await cur.fetchone()

    assert row is not None
    intent_id, run_at, summary, citations_json = row
    assert intent_id == 1
    assert run_at == "2026-05-26T09:00:00Z"
    assert summary == "Test summary"
    citations = json.loads(citations_json)
    assert len(citations) == 1
    assert citations[0]["article_id"] == "a1"
    assert citations[0]["score"] == 0.9


@pytest.mark.asyncio
async def test_save_summary_run_at_defaults_to_now(mem_conn) -> None:
    """When run_at is omitted, row gets a non-null ISO timestamp."""
    result = _result()
    row_id = await save_summary(mem_conn, result)

    async with mem_conn.execute("SELECT run_at FROM summary_history WHERE id=?", (row_id,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] and row[0].startswith("2026")


@pytest.mark.asyncio
async def test_save_summary_empty_citations(mem_conn) -> None:
    """Empty citations list serializes as '[]', not null."""
    result = _result(citations=[])
    row_id = await save_summary(mem_conn, result)

    async with mem_conn.execute(
        "SELECT citations FROM summary_history WHERE id=?", (row_id,)
    ) as cur:
        row = await cur.fetchone()
    assert json.loads(row[0]) == []


# ---------------------------------------------------------------------------
# format_history_text — query and formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_history_format_history_text_empty(mem_conn) -> None:
    """No rows → empty string."""
    text = await format_history_text(mem_conn, 1, 7)
    assert text == ""


@pytest.mark.asyncio
async def test_summary_history_format_history_text_multi(mem_conn) -> None:
    """Two rows → DESC-ordered '=== DATE ===' blocks."""
    await save_summary(mem_conn, _result(summary="first summary"), run_at="2026-05-24T09:00:00Z")
    await save_summary(mem_conn, _result(summary="second summary"), run_at="2026-05-25T09:00:00Z")

    text = await format_history_text(mem_conn, 1, 30)

    # Most recent first
    assert text.index("2026-05-25") < text.index("2026-05-24")
    assert "=== 2026-05-25 ===" in text
    assert "=== 2026-05-24 ===" in text
    assert "second summary" in text
    assert "first summary" in text
    # Blocks separated by double newline
    assert "\n\n" in text


@pytest.mark.asyncio
async def test_format_history_text_respects_history_days(mem_conn) -> None:
    """Only rows within history_days window are returned.

    Uses ``save_summary`` so the stored ``run_at`` matches the production
    ``%Y-%m-%dT%H:%M:%SZ`` format — the previous SQLite-side ``datetime('now',
    '-N days')`` produced a space-separated string that compares oddly against
    the new Python-computed anchor (T-separator) at sub-day resolution.
    """
    from datetime import datetime, timedelta  # noqa: PLC0415

    now = datetime.now(UTC)
    old_dt = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_dt = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await save_summary(mem_conn, _result(summary="old"), run_at=old_dt)
    await save_summary(mem_conn, _result(summary="recent"), run_at=recent_dt)

    text = await format_history_text(mem_conn, 1, 3)
    assert "recent" in text
    assert "old" not in text


@pytest.mark.asyncio
async def test_format_history_text_same_day_window_boundary(mem_conn) -> None:
    """Anchor and row on the same UTC date with sub-day diff — exact HH:MM compare."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    fixed_now = datetime(2026, 5, 24, 9, 0, 0, tzinfo=UTC)
    before_dt = (fixed_now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    after_dt = (fixed_now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await save_summary(mem_conn, _result(summary="just before"), run_at=before_dt)
    await save_summary(mem_conn, _result(summary="just after"), run_at=after_dt)

    text = await format_history_text(mem_conn, 1, 1, now=fixed_now)
    assert "just before" in text
    # Upper bound excludes rows after `now` — protects backfill replays.
    assert "just after" not in text


@pytest.mark.asyncio
async def test_format_history_text_guard_zero(mem_conn) -> None:
    """history_days < 1 returns '' without querying (P2 #13 guard)."""
    await save_summary(mem_conn, _result(), run_at="2026-05-26T09:00:00Z")
    text = await format_history_text(mem_conn, 1, 0)
    assert text == ""


@pytest.mark.asyncio
async def test_format_history_text_intent_isolation(mem_conn) -> None:
    """Rows from a different intent must not appear."""
    await save_summary(
        mem_conn, _result(intent_id=2, summary="other"), run_at="2026-05-26T09:00:00Z"
    )
    text = await format_history_text(mem_conn, 1, 30)
    assert text == ""


# ---------------------------------------------------------------------------
# CronSchedule.history_days — model validation
# ---------------------------------------------------------------------------


def test_cron_schedule_history_days_optional() -> None:
    """history_days defaults to None when not supplied."""
    from sembr.models import CronSchedule  # noqa: PLC0415

    s = CronSchedule(preset="daily", hour=9)
    assert s.history_days is None


def test_cron_schedule_history_days_roundtrip() -> None:
    """history_days=7 survives Pydantic round-trip."""
    from sembr.models import CronSchedule  # noqa: PLC0415

    s = CronSchedule(preset="daily", hour=9, history_days=7)
    assert s.history_days == 7
    dumped = s.model_dump()
    assert dumped["history_days"] == 7
    s2 = CronSchedule.model_validate(dumped)
    assert s2.history_days == 7


def test_cron_schedule_history_days_boundary() -> None:
    """history_days=0 and 366 fail Pydantic ge=1/le=365 constraints."""
    from pydantic import ValidationError  # noqa: PLC0415

    from sembr.models import CronSchedule  # noqa: PLC0415

    with pytest.raises(ValidationError):
        CronSchedule(preset="daily", history_days=0)
    with pytest.raises(ValidationError):
        CronSchedule(preset="daily", history_days=366)
    # Boundary values valid
    CronSchedule(preset="daily", history_days=1)
    CronSchedule(preset="daily", history_days=365)


def test_cron_schedule_history_days_null_in_json() -> None:
    """JSON with missing history_days key → Pydantic defaults to None (backward compat)."""
    from sembr.models import CronSchedule  # noqa: PLC0415

    s = CronSchedule.model_validate({"mode": "cron", "preset": "daily", "hour": 9, "minute": 0})
    assert s.history_days is None


# ---------------------------------------------------------------------------
# CASCADE delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_delete_cascade_history(mem_conn) -> None:
    """DELETE intent → summary_history rows CASCADE-deleted."""
    result = _result(intent_id=1)
    row_id = await save_summary(mem_conn, result, run_at="2026-05-26T09:00:00Z")

    # Confirm the row exists
    async with mem_conn.execute("SELECT id FROM summary_history WHERE id=?", (row_id,)) as cur:
        assert await cur.fetchone() is not None

    # Delete the intent — CASCADE must remove the summary_history row
    await mem_conn.execute("DELETE FROM intents WHERE id=1")
    await mem_conn.commit()

    async with mem_conn.execute("SELECT id FROM summary_history WHERE id=?", (row_id,)) as cur:
        assert await cur.fetchone() is None, "summary_history row should be cascade-deleted"

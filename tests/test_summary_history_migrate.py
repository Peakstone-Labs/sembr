# SPDX-License-Identifier: Apache-2.0
"""Tests for the new summary_history helpers + UNIQUE-index migration."""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiosqlite
import pytest

from sembr.db.sqlite import install_for_test
from sembr.db.summary_history import (
    delete_summary,
    format_history_text,
    init_summary_history_table,
    list_summaries,
    migrate_summary_history_unique_index,
    save_summary,
    save_summary_or_skip,
)
from sembr.summarizer.models import Citation, SummaryResult

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _result(intent_id: int = 1, summary: str = "summary text") -> SummaryResult:
    citation = Citation(
        article_id="a1",
        title="Article 1",
        url="https://example.com/1",
        source=10,
        published_at="2026-05-26T00:00:00Z",
        score=0.9,
    )
    return SummaryResult(
        intent_id=intent_id,
        summary=summary,
        citations=[citation],
        primary=citation,
        other_sources=[],
    )


@pytest.fixture()
async def mem_conn():
    from sembr.db.match_seen import init_match_seen_tables  # noqa: PLC0415

    async with aiosqlite.connect(":memory:") as conn:
        install_for_test(conn)
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            "CREATE TABLE intents (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)"
        )
        await conn.execute("INSERT INTO intents (id, name) VALUES (1, 'i1')")
        await conn.execute("INSERT INTO intents (id, name) VALUES (2, 'i2')")
        await conn.commit()
        await init_summary_history_table(conn)
        # match_seen is part of production lifespan; needed for delete_summary's
        # citation cascade (delete summary row -> evict its citations from
        # match_seen so future cron/backfill can re-match).
        await init_match_seen_tables(conn)
        yield conn


# ---------------------------------------------------------------------------
# migrate_summary_history_unique_index
# ---------------------------------------------------------------------------


async def test_migrate_unique_index_idempotent(mem_conn) -> None:
    """Calling the migration twice must not raise."""
    await migrate_summary_history_unique_index(mem_conn)
    await migrate_summary_history_unique_index(mem_conn)
    async with mem_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='uniq_summary_history_intent_run_at'"
    ) as cur:
        assert await cur.fetchone() is not None


async def test_migrate_unique_index_dedup_existing(mem_conn) -> None:
    """Pre-existing duplicate (intent_id, run_at) rows: keep MIN(id), drop the rest."""
    run_at = "2026-05-26T09:00:00Z"
    # Insert 3 rows with the same (intent_id, run_at) before the unique index exists.
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (1, ?, 'first', '[]')",
        (run_at,),
    )
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (1, ?, 'second', '[]')",
        (run_at,),
    )
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (1, ?, 'third', '[]')",
        (run_at,),
    )
    # A non-conflicting row should survive.
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (1, '2026-05-27T09:00:00Z', 'keep', '[]')"
    )
    await mem_conn.commit()

    await migrate_summary_history_unique_index(mem_conn)

    async with mem_conn.execute(
        "SELECT id, summary FROM summary_history WHERE intent_id=1 AND run_at=? ORDER BY id",
        (run_at,),
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][1] == "first"  # MIN(id) kept

    async with mem_conn.execute("SELECT COUNT(*) FROM summary_history WHERE intent_id=1") as cur:
        (count,) = await cur.fetchone()
    assert count == 2  # one deduplicated row + the non-conflicting "keep"


async def test_migrate_unique_index_no_data_loss(mem_conn) -> None:
    """No duplicates: row count unchanged after migration."""
    for i, run_at in enumerate(
        ["2026-05-24T09:00:00Z", "2026-05-25T09:00:00Z", "2026-05-26T09:00:00Z"]
    ):
        await save_summary(mem_conn, _result(summary=f"row{i}"), run_at=run_at)

    await migrate_summary_history_unique_index(mem_conn)

    async with mem_conn.execute("SELECT COUNT(*) FROM summary_history") as cur:
        (count,) = await cur.fetchone()
    assert count == 3


async def test_migrate_unique_index_enforces_constraint(mem_conn) -> None:
    """After migration, save_summary (plain INSERT) raises on duplicate (intent_id, run_at)."""
    import sqlite3  # noqa: PLC0415

    await migrate_summary_history_unique_index(mem_conn)
    run_at = "2026-05-26T09:00:00Z"
    await save_summary(mem_conn, _result(), run_at=run_at)
    with pytest.raises(sqlite3.IntegrityError):
        await save_summary(mem_conn, _result(), run_at=run_at)


# ---------------------------------------------------------------------------
# save_summary_or_skip
# ---------------------------------------------------------------------------


async def test_save_summary_or_skip_inserts_first(mem_conn) -> None:
    await migrate_summary_history_unique_index(mem_conn)
    inserted = await save_summary_or_skip(
        mem_conn, _result(summary="hello"), run_at="2026-05-26T09:00:00Z"
    )
    assert inserted is True
    async with mem_conn.execute("SELECT COUNT(*) FROM summary_history") as cur:
        (count,) = await cur.fetchone()
    assert count == 1


async def test_save_summary_or_skip_returns_false_on_conflict(mem_conn) -> None:
    await migrate_summary_history_unique_index(mem_conn)
    run_at = "2026-05-26T09:00:00Z"
    await save_summary_or_skip(mem_conn, _result(summary="first"), run_at=run_at)
    inserted_again = await save_summary_or_skip(mem_conn, _result(summary="second"), run_at=run_at)
    assert inserted_again is False
    # Table row count unchanged; first write preserved.
    async with mem_conn.execute(
        "SELECT summary FROM summary_history WHERE intent_id=1 AND run_at=?", (run_at,)
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "first"


async def test_save_summary_or_skip_run_at_format(mem_conn) -> None:
    """run_at written via save_summary_or_skip must match the project's ISO format."""
    await migrate_summary_history_unique_index(mem_conn)
    dt = datetime(2026, 5, 27, 9, 30, 15, tzinfo=UTC)
    run_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    await save_summary_or_skip(mem_conn, _result(), run_at=run_at)
    async with mem_conn.execute("SELECT run_at FROM summary_history WHERE intent_id=1") as cur:
        (stored,) = await cur.fetchone()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stored)
    assert stored == run_at


# ---------------------------------------------------------------------------
# list_summaries
# ---------------------------------------------------------------------------


async def test_list_summaries_desc_order(mem_conn) -> None:
    await save_summary(mem_conn, _result(summary="oldest"), run_at="2026-05-24T09:00:00Z")
    await save_summary(mem_conn, _result(summary="newest"), run_at="2026-05-26T09:00:00Z")
    await save_summary(mem_conn, _result(summary="middle"), run_at="2026-05-25T09:00:00Z")
    rows = await list_summaries(mem_conn, intent_id=1, limit=10)
    assert [r["summary"] for r in rows] == ["newest", "middle", "oldest"]


async def test_list_summaries_pagination(mem_conn) -> None:
    for i in range(5):
        await save_summary(
            mem_conn,
            _result(summary=f"r{i}"),
            run_at=f"2026-05-{20 + i:02d}T09:00:00Z",
        )
    page1 = await list_summaries(mem_conn, intent_id=1, limit=2, offset=0)
    page2 = await list_summaries(mem_conn, intent_id=1, limit=2, offset=2)
    page3 = await list_summaries(mem_conn, intent_id=1, limit=2, offset=4)
    combined = [r["summary"] for r in page1 + page2 + page3]
    assert combined == ["r4", "r3", "r2", "r1", "r0"]


async def test_list_summaries_intent_isolation(mem_conn) -> None:
    await save_summary(
        mem_conn, _result(intent_id=1, summary="i1-row"), run_at="2026-05-26T09:00:00Z"
    )
    await save_summary(
        mem_conn, _result(intent_id=2, summary="i2-row"), run_at="2026-05-26T09:00:00Z"
    )
    rows = await list_summaries(mem_conn, intent_id=1, limit=10)
    assert len(rows) == 1
    assert rows[0]["summary"] == "i1-row"


async def test_list_summaries_citations_parsed(mem_conn) -> None:
    await save_summary(mem_conn, _result(), run_at="2026-05-26T09:00:00Z")
    rows = await list_summaries(mem_conn, intent_id=1, limit=10)
    assert isinstance(rows[0]["citations"], list)
    assert rows[0]["citations"][0]["article_id"] == "a1"
    assert rows[0]["citations"][0]["score"] == 0.9


async def test_list_summaries_empty(mem_conn) -> None:
    assert await list_summaries(mem_conn, intent_id=1, limit=10) == []


# ---------------------------------------------------------------------------
# delete_summary
# ---------------------------------------------------------------------------


async def test_delete_summary_happy(mem_conn) -> None:
    row_id = await save_summary(mem_conn, _result(), run_at="2026-05-26T09:00:00Z")
    deleted = await delete_summary(mem_conn, intent_id=1, row_id=row_id)
    assert deleted is True
    rows = await list_summaries(mem_conn, intent_id=1, limit=10)
    assert rows == []


async def test_delete_summary_wrong_intent(mem_conn) -> None:
    """delete_summary must reject when row_id doesn't belong to intent_id."""
    row_id = await save_summary(mem_conn, _result(intent_id=1), run_at="2026-05-26T09:00:00Z")
    deleted = await delete_summary(mem_conn, intent_id=2, row_id=row_id)
    assert deleted is False
    # Row is still there for intent 1.
    rows = await list_summaries(mem_conn, intent_id=1, limit=10)
    assert len(rows) == 1


async def test_delete_summary_missing_row(mem_conn) -> None:
    deleted = await delete_summary(mem_conn, intent_id=1, row_id=99999)
    assert deleted is False


async def test_delete_summary_evicts_match_seen_for_citations(mem_conn) -> None:
    """DELETE history row also drops each citation's match_seen entry."""
    from sembr.summarizer.models import Citation  # noqa: PLC0415

    cites = [
        Citation(
            article_id=f"art-{i}",
            title=f"t{i}",
            url=f"https://x/{i}",
            source=1,
            published_at="2026-05-26T00:00:00Z",
            score=0.9,
        )
        for i in range(3)
    ]
    result = SummaryResult(
        intent_id=1,
        summary="s",
        citations=cites,
        primary=cites[0],
        other_sources=cites[1:],
    )
    row_id = await save_summary(mem_conn, result, run_at="2026-05-26T09:00:00Z")
    # Pre-seed match_seen for all 3 + an unrelated article that must survive
    await mem_conn.execute(
        "INSERT INTO match_seen (intent_id, article_id) VALUES "
        "(1, 'art-0'), (1, 'art-1'), (1, 'art-2'), (1, 'keep-me')"
    )
    await mem_conn.execute("INSERT INTO match_seen (intent_id, article_id) VALUES (2, 'art-0')")
    await mem_conn.commit()

    deleted = await delete_summary(mem_conn, intent_id=1, row_id=row_id)
    assert deleted is True

    async with mem_conn.execute(
        "SELECT article_id FROM match_seen WHERE intent_id=1 ORDER BY article_id"
    ) as cur:
        rows = [r[0] for r in await cur.fetchall()]
    assert rows == ["keep-me"], f"only the unrelated article should survive; got {rows}"
    # Other intent's row for the same article_id must NOT be touched
    async with mem_conn.execute("SELECT article_id FROM match_seen WHERE intent_id=2") as cur:
        other = await cur.fetchall()
    assert other == [("art-0",)]


# ---------------------------------------------------------------------------
# format_history_text with now= parameter
# ---------------------------------------------------------------------------


async def test_format_history_text_now_param_anchored(mem_conn) -> None:
    """Lower-bound anchor = now - history_days."""
    # 5 rows spanning the anchor
    await save_summary(mem_conn, _result(summary="too-old"), run_at="2026-03-20T00:00:00Z")
    await save_summary(
        mem_conn, _result(summary="on-or-after-anchor"), run_at="2026-03-26T00:00:00Z"
    )
    await save_summary(mem_conn, _result(summary="closer"), run_at="2026-03-30T00:00:00Z")
    await save_summary(mem_conn, _result(summary="at-anchor"), run_at="2026-03-25T00:00:00Z")
    now = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    text = await format_history_text(mem_conn, 1, history_days=7, now=now)
    assert "closer" in text
    assert "on-or-after-anchor" in text
    assert "at-anchor" in text  # boundary inclusive (>= anchor)
    assert "too-old" not in text


async def test_format_history_text_upper_bound(mem_conn) -> None:
    """Upper-bound anchor = now (excludes rows from after the simulated past fire)."""
    await save_summary(mem_conn, _result(summary="before"), run_at="2026-03-30T00:00:00Z")
    await save_summary(mem_conn, _result(summary="at-now"), run_at="2026-04-01T09:00:00Z")
    await save_summary(mem_conn, _result(summary="future"), run_at="2026-05-15T00:00:00Z")
    now = datetime(2026, 4, 1, 9, 0, 0, tzinfo=UTC)
    text = await format_history_text(mem_conn, 1, history_days=7, now=now)
    assert "before" in text
    assert "at-now" in text
    assert "future" not in text


async def test_format_history_text_now_default_real_now(mem_conn) -> None:
    """Without now=, behaviour equivalent to original: rows within history_days are returned."""
    # Use SQLite's clock to produce a guaranteed-fresh row.
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) "
        "VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 day'), 'recent', '[]')"
    )
    await mem_conn.execute(
        "INSERT INTO summary_history (intent_id, run_at, summary, citations) "
        "VALUES (1, strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 days'), 'old', '[]')"
    )
    await mem_conn.commit()
    text = await format_history_text(mem_conn, 1, history_days=3)
    assert "recent" in text
    assert "old" not in text


async def test_format_history_text_guard_zero_days(mem_conn) -> None:
    await save_summary(mem_conn, _result(), run_at="2026-05-26T09:00:00Z")
    assert await format_history_text(mem_conn, 1, history_days=0) == ""


async def test_format_history_text_intent_isolation(mem_conn) -> None:
    await save_summary(
        mem_conn, _result(intent_id=2, summary="other"), run_at="2026-03-30T00:00:00Z"
    )
    now = datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    text = await format_history_text(mem_conn, 1, history_days=30, now=now)
    assert text == ""

"""Loop polish: list_feed_events supports offset for inline-expand pagination.

Companion to the Feeds tab UI change that limits drill-down to 10 rows + prev/next.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sembr.dashboard import read_model
from sembr.dashboard.events import init_event_log_tables, log_fetch_event
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "sembr.db")
    conn = await init_sqlite(path)
    await init_feed_tables(conn)
    await init_event_log_tables(conn)
    await conn.execute(
        "INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://example.com')"
    )
    await conn.commit()
    # Seed 25 fetch events; ORDER BY id DESC → newest first.
    for i in range(25):
        await log_fetch_event(
            feed_id=1,
            started_at=datetime.now(timezone.utc),
            elapsed_ms=10 + i,
            ok=True,
            items_seen=i,
            items_new=0,
            error_class=None,
            error_message=None,
        )
    yield conn
    await close_sqlite()


@pytest.mark.asyncio
async def test_list_feed_events_default_offset_zero(db) -> None:
    rows = await read_model.list_feed_events(db, 1, limit=10)
    assert len(rows) == 10
    # newest-first: elapsed_ms 34, 33, … 25
    assert rows[0].elapsed_ms == 34
    assert rows[-1].elapsed_ms == 25


@pytest.mark.asyncio
async def test_list_feed_events_offset_skips_first_page(db) -> None:
    rows = await read_model.list_feed_events(db, 1, limit=10, offset=10)
    assert len(rows) == 10
    assert rows[0].elapsed_ms == 24
    assert rows[-1].elapsed_ms == 15


@pytest.mark.asyncio
async def test_list_feed_events_offset_past_end_returns_remainder(db) -> None:
    rows = await read_model.list_feed_events(db, 1, limit=10, offset=20)
    assert len(rows) == 5  # only 5 rows past offset 20 (25 total)


@pytest.mark.asyncio
async def test_list_feed_events_offset_beyond_returns_empty(db) -> None:
    rows = await read_model.list_feed_events(db, 1, limit=10, offset=100)
    assert rows == []

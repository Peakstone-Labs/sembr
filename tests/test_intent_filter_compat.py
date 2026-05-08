"""Regression: source_type='newsapi' feeds must be treated identically to
RSS feeds by Intent.feed_filter — feed_filter only stores integer feed_ids,
so the source_type column is opaque to it. This test pins that invariant
so future refactors don't accidentally couple feed_filter to source_type.
"""
from __future__ import annotations

import aiosqlite
import pytest

from sembr.db.feeds import init_feed_tables
from sembr.db.intents import (
    create_intent,
    get_intent,
    init_intent_tables,
    intents_remove_feed_id,
)
from sembr.db.sqlite import install_for_test
from sembr.models import FeedCreate, FeedFilter, IntentCreate


@pytest.fixture
async def mem_conn(monkeypatch):
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    monkeypatch.setenv("NEWSAPI_POLL_INTERVAL_MINUTES", "30")
    from sembr.config import get_settings
    get_settings.cache_clear()
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_intent_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


async def _insert_feed_row(conn, *, fid: int, source_type: str, url: str) -> None:
    await conn.execute(
        "INSERT INTO feeds (id, name, url, source_type, enabled) VALUES (?, ?, ?, ?, 1)",
        (fid, f"feed-{fid}", url, source_type),
    )
    await conn.commit()


@pytest.mark.asyncio
async def test_newsapi_feed_id_routes_through_feed_filter(mem_conn) -> None:
    """Intent feed_filter.ids accepts any feed_id regardless of source_type."""
    await _insert_feed_row(mem_conn, fid=10, source_type="newsapi", url="reuters.com")
    await _insert_feed_row(mem_conn, fid=11, source_type="rss", url="http://example.com/rss")

    intent = await create_intent(
        mem_conn,
        IntentCreate(
            name="t",
            text="x",
            channels=[{"type": "email", "to": ["a@example.com"]}],
            feed_filter=FeedFilter(ids=[10, 11]),
        ),
    )
    persisted = await get_intent(mem_conn, intent.id)
    assert persisted is not None
    assert persisted.feed_filter is not None
    assert sorted(persisted.feed_filter.ids) == [10, 11]


@pytest.mark.asyncio
async def test_newsapi_feed_delete_cascades_into_intent_feed_filter(mem_conn) -> None:
    """Same intents_remove_feed_id machinery as RSS — D-Goal #5."""
    await _insert_feed_row(mem_conn, fid=20, source_type="newsapi", url="reuters.com")
    intent = await create_intent(
        mem_conn,
        IntentCreate(
            name="t",
            text="x",
            channels=[{"type": "email", "to": ["a@example.com"]}],
            feed_filter=FeedFilter(ids=[20, 21]),
        ),
    )
    affected = await intents_remove_feed_id(mem_conn, 20)
    await mem_conn.commit()
    assert intent.id in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is not None
    assert updated.feed_filter.ids == [21]


@pytest.mark.asyncio
async def test_feedcreate_newsapi_round_trip_into_feeds_table(mem_conn) -> None:
    """FeedCreate validation produces a hostname-only url; insert and round-trip
    via the feeds table preserves it (UNIQUE constraint catches duplicates)."""
    body = FeedCreate(name="Reuters", url="HTTPS://www.Reuters.com/", source_type="newsapi")
    assert body.url == "reuters.com"
    await mem_conn.execute(
        "INSERT INTO feeds (name, url, source_type, enabled) VALUES (?, ?, ?, 1)",
        (body.name, body.url, body.source_type),
    )
    await mem_conn.commit()

    # Duplicate insert via different casing → UNIQUE violation after re-validate.
    body2 = FeedCreate(name="dup", url="reuters.com", source_type="newsapi")
    with pytest.raises(aiosqlite.IntegrityError):
        await mem_conn.execute(
            "INSERT INTO feeds (name, url, source_type, enabled) VALUES (?, ?, ?, 1)",
            (body2.name, body2.url, body2.source_type),
        )
        await mem_conn.commit()

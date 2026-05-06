"""Unit tests for DD3: feed delete cascade — intents_remove_feed_id."""
from __future__ import annotations

import json

import aiosqlite
import pytest

from sembr.db.intents import create_intent, get_intent, init_intent_tables, intents_remove_feed_id
from sembr.db.sqlite import install_for_test
from sembr.models import FeedFilter, IntentCreate


def _intent_with_filter(ids: list[int] | None) -> IntentCreate:
    return IntentCreate(
        name="cascade-test",
        text="cascade test intent",
        channels=[{"type": "email", "to": ["a@example.com"]}],
        feed_filter=FeedFilter(ids=ids) if ids is not None else None,
    )


@pytest.fixture
async def mem_conn():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_remove_feed_id_from_subset(mem_conn) -> None:
    """Removing feed_id=2 from [1,2,3] leaves [1,3]."""
    intent = await create_intent(mem_conn, _intent_with_filter([1, 2, 3]))
    affected = await intents_remove_feed_id(mem_conn, 2)
    await mem_conn.commit()

    assert intent.id in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is not None
    assert sorted(updated.feed_filter.ids) == [1, 3]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_remove_feed_id_leaves_empty_list(mem_conn) -> None:
    """Removing the only feed_id leaves ids=[]."""
    intent = await create_intent(mem_conn, _intent_with_filter([5]))
    affected = await intents_remove_feed_id(mem_conn, 5)
    await mem_conn.commit()

    assert intent.id in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is not None
    assert updated.feed_filter.ids == []


@pytest.mark.asyncio
async def test_remove_feed_id_from_null_filter_unchanged(mem_conn) -> None:
    """feed_filter=null (全扫) is not touched by cascade."""
    intent = await create_intent(mem_conn, _intent_with_filter(None))
    affected = await intents_remove_feed_id(mem_conn, 1)
    await mem_conn.commit()

    assert intent.id not in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is None


@pytest.mark.asyncio
async def test_remove_feed_id_from_empty_ids_unchanged(mem_conn) -> None:
    """feed_filter.ids=[] (empty set) is not touched by cascade."""
    intent = await create_intent(mem_conn, _intent_with_filter([]))
    affected = await intents_remove_feed_id(mem_conn, 1)
    await mem_conn.commit()

    assert intent.id not in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is not None
    assert updated.feed_filter.ids == []


@pytest.mark.asyncio
async def test_remove_feed_id_not_in_list_unchanged(mem_conn) -> None:
    """Removing a feed_id not in the list does not change the intent."""
    intent = await create_intent(mem_conn, _intent_with_filter([1, 3]))
    affected = await intents_remove_feed_id(mem_conn, 99)
    await mem_conn.commit()

    assert intent.id not in affected
    updated = await get_intent(mem_conn, intent.id)
    assert updated is not None
    assert updated.feed_filter is not None
    assert sorted(updated.feed_filter.ids) == [1, 3]  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_remove_feed_id_multiple_intents(mem_conn) -> None:
    """All intents referencing the feed_id get updated; others unaffected."""
    i1 = await create_intent(mem_conn, _intent_with_filter([1, 2]))
    i2 = await create_intent(mem_conn, _intent_with_filter([2, 3]))
    i3 = await create_intent(mem_conn, _intent_with_filter([3, 4]))

    affected = await intents_remove_feed_id(mem_conn, 2)
    await mem_conn.commit()

    assert i1.id in affected
    assert i2.id in affected
    assert i3.id not in affected

    u1 = await get_intent(mem_conn, i1.id)
    u2 = await get_intent(mem_conn, i2.id)
    u3 = await get_intent(mem_conn, i3.id)

    assert u1.feed_filter.ids == [1]  # type: ignore[union-attr]
    assert u2.feed_filter.ids == [3]  # type: ignore[union-attr]
    assert sorted(u3.feed_filter.ids) == [3, 4]  # type: ignore[union-attr,arg-type]

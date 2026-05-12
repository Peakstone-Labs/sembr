# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr/matcher/event_buffer.py — absorb, flush, sweep_timed_out."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.db.event_buffer import init_event_buffer_tables
from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.sqlite import install_for_test
from sembr.matcher.callback import Match
from sembr.matcher.event_buffer import absorb, flush, sweep_timed_out
from sembr.models import EventSchedule, IntentCreate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EVENT_SCHEDULE = EventSchedule(trigger_count=3, max_wait_seconds=60)

_INTENT_BODY = IntentCreate(
    name="event-test",
    text="quantum computing",
    channels=[{"type": "email", "to": ["a@example.com"]}],
    schedule=_EVENT_SCHEDULE,
)


def _match(article_id: str, title: str = "test title", score: float = 0.85) -> Match:
    return Match(
        intent_id=1,
        article_id=article_id,
        score=score,
        payload={
            "title": title,
            "url": "https://example.com",
            "body": "",
            "feed_id": 1,
            "published_at": None,
        },
    )


async def _setup_db() -> tuple[aiosqlite.Connection, int]:
    """Open in-memory DB, init tables, create one event-mode intent; return (conn, intent_id)."""
    conn = await aiosqlite.connect(":memory:")
    await init_intent_tables(conn)
    await init_event_buffer_tables(conn)
    install_for_test(conn)
    intent = await create_intent(conn, _INTENT_BODY)
    return conn, intent.id


# ---------------------------------------------------------------------------
# absorb — batch grouping and counting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absorb_empty_buffer_single_batch_creates_one_group():
    """5 matches with similar titles → 1 group in event_pending."""
    conn, intent_id = await _setup_db()
    try:
        matches = [_match(f"art-{i}", "Apple iPhone 16 launch event announced") for i in range(5)]
        should_flush = await absorb(conn, intent_id, matches, _EVENT_SCHEDULE)
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 1
        assert should_flush is False  # 1 group < trigger_count=3
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_absorb_cross_batch_merge_similar_title():
    """Existing group 'Apple event' + new batch 'Apple iPhone launch' (≥0.85) → still 1 group."""
    conn, intent_id = await _setup_db()
    try:
        await absorb(conn, intent_id, [_match("art-1", "Apple product event")], _EVENT_SCHEDULE)
        await absorb(conn, intent_id, [_match("art-2", "Apple product event day")], _EVENT_SCHEDULE)
        async with conn.execute(
            "SELECT COUNT(*), members_json FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1, "similar-title batches should merge into one group"
        members = json.loads(row[1])
        assert len(members) == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_absorb_distinct_titles_creates_separate_groups():
    """Two batches with completely different titles → 2 groups."""
    conn, intent_id = await _setup_db()
    try:
        await absorb(conn, intent_id, [_match("art-1", "Apple iPhone event")], _EVENT_SCHEDULE)
        await absorb(
            conn, intent_id, [_match("art-2", "TSMC semiconductor fab expansion")], _EVENT_SCHEDULE
        )
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_absorb_article_id_dedup_on_retry():
    """Same article_id submitted twice (Risk 2 retry scenario) → members_json not duplicated."""
    conn, intent_id = await _setup_db()
    try:
        m = _match("art-dup", "repeated article title")
        await absorb(conn, intent_id, [m], _EVENT_SCHEDULE)
        await absorb(conn, intent_id, [m], _EVENT_SCHEDULE)  # retry
        async with conn.execute(
            "SELECT members_json FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (raw,) = await cur.fetchone()
        members = json.loads(raw)
        ids = [x["article_id"] for x in members]
        assert ids.count("art-dup") == 1, "duplicate article_id must not be added twice"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_absorb_returns_should_flush_when_count_reaches_trigger():
    """3 distinct groups → absorb returns True (trigger_count=3)."""
    conn, intent_id = await _setup_db()
    try:
        await absorb(conn, intent_id, [_match("art-1", "Topic Alpha release")], _EVENT_SCHEDULE)
        await absorb(conn, intent_id, [_match("art-2", "Topic Beta announcement")], _EVENT_SCHEDULE)
        result = await absorb(
            conn, intent_id, [_match("art-3", "Topic Gamma update")], _EVENT_SCHEDULE
        )
        assert result is True
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_absorb_empty_matches_returns_false():
    """Empty batch is a no-op; should_flush=False."""
    conn, intent_id = await _setup_db()
    try:
        result = await absorb(conn, intent_id, [], _EVENT_SCHEDULE)
        assert result is False
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 0
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# flush — atomicity and E1 contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_drains_all_groups_and_calls_on_match():
    """flush() empties event_pending and calls on_match once."""
    conn, intent_id = await _setup_db()
    on_match = AsyncMock()
    app = MagicMock()
    app.state.on_match = on_match

    try:
        # Insert 2 distinct groups
        await absorb(conn, intent_id, [_match("art-1", "Story One publish")], _EVENT_SCHEDULE)
        await absorb(conn, intent_id, [_match("art-2", "Story Two launch")], _EVENT_SCHEDULE)

        await flush(conn, app, intent_id)

        # Buffer must be empty
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 0
        on_match.assert_awaited_once()
        matches_passed = on_match.await_args[0][0]
        assert len(matches_passed) == 2
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_flush_no_rows_is_noop():
    """flush() on empty intent_id returns without error and does not call on_match."""
    conn, intent_id = await _setup_db()
    on_match = AsyncMock()
    app = MagicMock()
    app.state.on_match = on_match

    try:
        await flush(conn, app, intent_id)
        on_match.assert_not_awaited()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_flush_on_match_exception_does_not_reraise(caplog):
    """E1 contract: on_match failure is logged at WARNING but NOT re-raised; buffer already cleared."""
    import logging

    conn, intent_id = await _setup_db()
    on_match = AsyncMock(side_effect=RuntimeError("push failed"))
    app = MagicMock()
    app.state.on_match = on_match

    try:
        await absorb(conn, intent_id, [_match("art-1", "Breaking news")], _EVENT_SCHEDULE)
        with caplog.at_level(logging.WARNING, logger="sembr.matcher.event_buffer"):
            # Must not raise
            await flush(conn, app, intent_id)
        # Buffer still empty — DELETE committed before on_match was called
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (intent_id,)
        ) as cur:
            (count,) = await cur.fetchone()
        assert count == 0
        assert any(
            "on_match raised" in rec.message and rec.levelname == "WARNING"
            for rec in caplog.records
        ), "E1: on_match failure must be logged at WARNING"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# sweep_timed_out — Y-trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_triggers_flush_when_oldest_group_exceeds_max_wait():
    """Buffer older than max_wait_seconds → sweep calls flush."""
    from sembr.matcher.event_cache import EventIntentCache, EventIntentEntry

    conn, intent_id = await _setup_db()
    on_match = AsyncMock()
    app = MagicMock()
    app.state.on_match = on_match

    cache = EventIntentCache()
    cache.add(
        intent_id,
        EventIntentEntry(
            vectors={"main": [0.1] * 1024},
            threshold=0.75,
            feed_filter_ids=None,
            schedule=EventSchedule(trigger_count=10, max_wait_seconds=60),
        ),
    )

    try:
        await absorb(
            conn,
            intent_id,
            [_match("art-1", "Old news")],
            EventSchedule(trigger_count=10, max_wait_seconds=60),
        )
        # Backdate the created_at to simulate timeout
        past = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
        await conn.execute(
            "UPDATE event_pending SET created_at=? WHERE intent_id=?", (past, intent_id)
        )
        await conn.commit()

        await sweep_timed_out(conn, app, cache)
        on_match.assert_awaited_once()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_sweep_does_not_flush_when_not_yet_timed_out():
    """Buffer newer than max_wait_seconds → sweep does nothing."""
    from sembr.matcher.event_cache import EventIntentCache, EventIntentEntry

    conn, intent_id = await _setup_db()
    on_match = AsyncMock()
    app = MagicMock()
    app.state.on_match = on_match

    cache = EventIntentCache()
    cache.add(
        intent_id,
        EventIntentEntry(
            vectors={"main": [0.1] * 1024},
            threshold=0.75,
            feed_filter_ids=None,
            schedule=EventSchedule(trigger_count=10, max_wait_seconds=3600),
        ),
    )

    try:
        await absorb(
            conn,
            intent_id,
            [_match("art-1", "Recent news")],
            EventSchedule(trigger_count=10, max_wait_seconds=3600),
        )
        await sweep_timed_out(conn, app, cache)
        on_match.assert_not_awaited()
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_sweep_isolates_per_intent_failures():
    """One intent flush failure does not abort remaining intents."""
    from sembr.matcher.event_cache import EventIntentCache, EventIntentEntry

    conn = await aiosqlite.connect(":memory:")
    await init_intent_tables(conn)
    await init_event_buffer_tables(conn)
    install_for_test(conn)

    # Create two intents
    body1 = IntentCreate(
        name="intent-1",
        text="topic1",
        channels=[{"type": "email", "to": ["a@example.com"]}],
        schedule=EventSchedule(trigger_count=10, max_wait_seconds=60),
    )
    body2 = IntentCreate(
        name="intent-2",
        text="topic2",
        channels=[{"type": "email", "to": ["a@example.com"]}],
        schedule=EventSchedule(trigger_count=10, max_wait_seconds=60),
    )
    i1 = await create_intent(conn, body1)
    i2 = await create_intent(conn, body2)

    cache = EventIntentCache()
    for iid in [i1.id, i2.id]:
        cache.add(
            iid,
            EventIntentEntry(
                vectors={"main": [0.1] * 1024},
                threshold=0.75,
                feed_filter_ids=None,
                schedule=EventSchedule(trigger_count=10, max_wait_seconds=60),
            ),
        )

    # absorb first (each call uses BEGIN IMMEDIATE / COMMIT internally)
    for iid in [i1.id, i2.id]:
        m = Match(
            intent_id=iid,
            article_id=f"art-{iid}",
            score=0.9,
            payload={"title": "Test", "url": "", "body": "", "feed_id": 1, "published_at": None},
        )
        await absorb(conn, iid, [m], EventSchedule(trigger_count=10, max_wait_seconds=60))

    # Backdate both in a single transaction after all absorbs are committed
    past = (datetime.now(timezone.utc) - timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for iid in [i1.id, i2.id]:
        await conn.execute("UPDATE event_pending SET created_at=? WHERE intent_id=?", (past, iid))
    await conn.commit()

    # First intent on_match raises; second should still fire
    call_log: list[int] = []

    async def _on_match(matches):
        iid = matches[0].intent_id
        call_log.append(iid)
        if iid == i1.id:
            raise RuntimeError("intentional failure")

    app = MagicMock()
    app.state.on_match = _on_match

    try:
        await sweep_timed_out(conn, app, cache)
        # Both intents must be attempted regardless of SQLite row order (no ORDER BY in sweep)
        assert sorted(call_log) == sorted([i1.id, i2.id]), (
            f"both intents must be attempted despite i1 raising; got call_log={call_log}"
        )
        # i2 buffer must actually be drained (strongest order-independent check)
        async with conn.execute(
            "SELECT COUNT(*) FROM event_pending WHERE intent_id=?", (i2.id,)
        ) as cur:
            (remaining,) = await cur.fetchone()
        assert remaining == 0, "i2 buffer must be drained even though i1 raised"
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# _dot and event_match_batch — Risk 7 regression guard
# ---------------------------------------------------------------------------


def test_dot_length_mismatch_raises():
    """Loop 4 🟡4 guard: _dot must raise ValueError on mismatched vector lengths."""
    from sembr.matcher.event_match import _dot

    with pytest.raises(ValueError, match="vector length mismatch"):
        _dot([1.0, 2.0], [1.0, 2.0, 3.0])


@pytest.mark.asyncio
async def test_event_match_batch_swallows_dot_mismatch(caplog):
    """Risk 7: length-mismatched cache entry must not abort the batch; logs WARNING."""
    import logging

    from sembr.matcher.event_cache import EventIntentCache, EventIntentEntry
    from sembr.matcher.event_match import event_match_batch

    conn, intent_id = await _setup_db()
    try:
        # Cache entry with wrong vector length (512 instead of 1024)
        cache = EventIntentCache()
        cache.add(
            intent_id,
            EventIntentEntry(
                vectors={"main": [0.1] * 512},  # mismatched — article points will be 1024-dim
                threshold=0.75,
                feed_filter_ids=None,
                schedule=_EVENT_SCHEDULE,
            ),
        )

        app = MagicMock()
        app.state.event_intent_cache = cache
        app.state.db_conn = conn

        # Build a fake Qdrant point with 1024-dim vector
        point = MagicMock()
        point.id = "art-mismatch"
        point.vector = [0.5] * 1024
        point.payload = {
            "title": "Test article",
            "url": "https://example.com",
            "body": "",
            "feed_id": 1,
            "published_at": None,
        }

        with caplog.at_level(logging.WARNING, logger="sembr.matcher.event_match"):
            await event_match_batch(app, [point], conn)

        assert any(rec.levelname == "WARNING" for rec in caplog.records), (
            "Risk 7: event_match_batch must log WARNING on internal failure"
        )
    finally:
        await conn.close()

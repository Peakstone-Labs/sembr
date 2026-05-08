"""Unit tests for manual prune state machine (S6 + S7 + S8 + correctness)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.maintenance import manual_prune
from sembr.maintenance import tasks as mp_tasks
from sembr.vector_store.news import md5_to_uuid


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    return conn


async def _seed_feed(conn, feed_id: int, name: str = "F") -> None:
    await conn.execute(
        "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
        "VALUES (?, ?, ?, 30)",
        (feed_id, name, f"http://x/{feed_id}"),
    )
    await conn.commit()


async def _seed_dead_row(
    conn, md5: str, feed_id: int | None, failed_at_iso: str
) -> None:
    await conn.execute(
        "INSERT INTO dead_articles "
        "(md5, feed_id, url, title, body, published_at, error_message, failed_at) "
        "VALUES (?, ?, 'u', 't', 'b', NULL, 'err', ?)",
        (md5, feed_id, failed_at_iso),
    )
    await conn.commit()


def _make_qdrant(facet_counts: dict[int, int] | None = None) -> MagicMock:
    """Stub qdrant whose facet() returns the given counts."""
    q = MagicMock()
    facet_counts = facet_counts or {}

    async def fake_facet(**kwargs):
        res = MagicMock()
        hits = []
        for fid, cnt in facet_counts.items():
            h = MagicMock()
            h.value = fid
            h.count = cnt
            hits.append(h)
        res.hits = hits
        return res

    q.client.facet = AsyncMock(side_effect=fake_facet)
    q.client.scroll = AsyncMock(return_value=([], None))
    q.client.delete = AsyncMock()
    return q


@pytest.fixture(autouse=True)
def _reset_tasks():
    mp_tasks._reset_for_testing()
    yield
    mp_tasks._reset_for_testing()


@pytest.mark.asyncio
async def test_planning_news_fills_plan_summary():
    conn = await _make_conn()
    await _seed_feed(conn, 6, "CNN Edition")
    await _seed_feed(conn, 12, "Economist")

    qdrant = _make_qdrant({6: 12, 12: 88})
    task = mp_tasks.create_task("news", [6, 12], 35)
    await manual_prune.run_planning(task, qdrant)

    assert task.status == "planned"
    assert task.plan_summary["target"] == "news"
    assert task.plan_summary["older_than_days"] == 35
    assert task.plan_summary["total_would_delete"] == 100
    feeds_by_id = {f["feed_id"]: f for f in task.plan_summary["feeds"]}
    assert feeds_by_id[6]["would_delete"] == 12
    assert feeds_by_id[6]["feed_name"] == "CNN Edition"
    assert feeds_by_id[6]["deleted"] is False
    assert feeds_by_id[12]["would_delete"] == 88

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_planning_includes_deleted_feed():
    """S7: feed_id 99 is in Qdrant but not in feeds — must surface as deleted=True."""
    conn = await _make_conn()
    await _seed_feed(conn, 6, "Real")

    # 99 has 34 expired points; not in feeds table → "deleted feed"
    qdrant = _make_qdrant({6: 5, 99: 34})
    task = mp_tasks.create_task("news", [6, 99], 35)
    await manual_prune.run_planning(task, qdrant)

    feeds_by_id = {f["feed_id"]: f for f in task.plan_summary["feeds"]}
    assert feeds_by_id[99]["deleted"] is True
    assert feeds_by_id[99]["feed_name"] is None
    assert feeds_by_id[99]["would_delete"] == 34

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_planning_dead_uses_sqlite_groupby():
    conn = await _make_conn()
    await _seed_feed(conn, 7)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=20)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()
    for i in range(3):
        await _seed_dead_row(conn, f"{i:032x}", 7, old)
    for i in range(3, 5):
        await _seed_dead_row(conn, f"{i:032x}", 7, fresh)
    await _seed_dead_row(conn, "f" * 32, 99, old)  # deleted feed_id

    task = mp_tasks.create_task("dead", [7, 99], 14)
    await manual_prune.run_planning(task, qdrant_handle=None)

    feeds_by_id = {f["feed_id"]: f for f in task.plan_summary["feeds"]}
    assert feeds_by_id[7]["would_delete"] == 3
    assert feeds_by_id[99]["would_delete"] == 1
    assert feeds_by_id[99]["deleted"] is True
    assert task.plan_summary["total_would_delete"] == 4

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_apply_dead_deletes_only_old():
    """S8 dead branch: actual delete only touches dead_articles, not feed_items."""
    conn = await _make_conn()
    await _seed_feed(conn, 7)
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=20)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()
    for i in range(3):
        await _seed_dead_row(conn, f"{i:032x}", 7, old)
    for i in range(3, 5):
        await _seed_dead_row(conn, f"{i:032x}", 7, fresh)

    task = mp_tasks.create_task("dead", [7], 14)
    await manual_prune.run_planning(task, qdrant_handle=None)
    await manual_prune.run_applying(task, qdrant_handle=None)

    assert task.status == "done"
    assert task.result_summary["deleted_dead_articles"] == 3
    async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
        assert (await cur.fetchone())[0] == 2

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_apply_news_deletes_qdrant_and_cascade():
    """S8 news branch: qdrant point + feed_items + match_seen all gone after apply."""
    conn = await _make_conn()
    await _seed_feed(conn, 6)

    md5s = [f"{i:032x}" for i in range(3)]
    for m in md5s:
        await conn.execute(
            "INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (m, 6)
        )
    await conn.commit()

    expired_uuids = [md5_to_uuid(m) for m in md5s]

    qdrant = _make_qdrant({6: 3})
    # scroll returns the three uuids in one page
    points = []
    for u in expired_uuids:
        p = MagicMock()
        p.id = u
        points.append(p)
    qdrant.client.scroll = AsyncMock(return_value=(points, None))

    task = mp_tasks.create_task("news", [6], 35)
    await manual_prune.run_planning(task, qdrant)
    await manual_prune.run_applying(task, qdrant)

    assert task.status == "done"
    assert task.result_summary["deleted_qdrant"] == 3
    assert task.result_summary["deleted_feed_items"] == 3
    qdrant.client.delete.assert_awaited()
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        assert (await cur.fetchone())[0] == 0

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_planning_error_path_records_error():
    """If facet() raises, status flips to error with error message."""
    conn = await _make_conn()
    await _seed_feed(conn, 6)

    q = MagicMock()
    q.client.facet = AsyncMock(side_effect=RuntimeError("qdrant down"))
    task = mp_tasks.create_task("news", [6], 35)
    await manual_prune.run_planning(task, q)

    assert task.status == "error"
    assert "qdrant down" in (task.error or "")
    assert task.plan_summary is None

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


# ---------------------------------------------------------------------------
# sweep_expired status filtering (Loop 2 🟡-3 regression)
# ---------------------------------------------------------------------------


def test_sweep_skips_in_flight_planning_task():
    """In-flight planning tasks must survive sweep regardless of age — see
    review.md Loop 1 finding 🟡-3.
    """
    task = mp_tasks.create_task("news", [6], 35)
    # status="planning" out of the box; force _created_at to look ancient
    task._created_at = datetime.now(timezone.utc) - timedelta(seconds=400)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 0
    assert mp_tasks.get_task(task.task_id) is not None


def test_sweep_skips_in_flight_applying_task():
    task = mp_tasks.create_task("dead", [7], 14)
    task.status = "applying"
    task._created_at = datetime.now(timezone.utc) - timedelta(seconds=10000)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 0
    assert mp_tasks.get_task(task.task_id) is not None


def test_sweep_skips_planned_task_awaiting_user_confirm():
    """`planned` is the state where dry-run has finished and the user is
    reading the per-feed numbers. They may step away from the screen for
    longer than the TTL — sweep must NOT clear the task or the next click
    on Confirm hits 404.
    """
    task = mp_tasks.create_task("news", [6], 35)
    task.status = "planned"
    task.plan_summary = {
        "target": "news", "older_than_days": 35,
        "feeds": [], "total_would_delete": 0,
    }
    task._created_at = datetime.now(timezone.utc) - timedelta(seconds=10000)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 0
    assert mp_tasks.get_task(task.task_id) is not None


def test_sweep_drops_done_task_using_finished_at_anchor():
    task = mp_tasks.create_task("dead", [7], 14)
    task.status = "done"
    # _created_at is fresh, but finished_at is stale → must drop
    task._created_at = datetime.now(timezone.utc)
    task.finished_at = datetime.now(timezone.utc) - timedelta(seconds=400)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 1
    assert mp_tasks.get_task(task.task_id) is None


def test_sweep_keeps_recently_done_task():
    task = mp_tasks.create_task("dead", [7], 14)
    task.status = "done"
    task.finished_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 0
    assert mp_tasks.get_task(task.task_id) is not None


def test_sweep_drops_error_task_using_created_at_when_no_finished():
    """Error tasks that never set finished_at fall back to _created_at."""
    task = mp_tasks.create_task("news", [6], 35)
    task.status = "error"
    task.finished_at = None
    task._created_at = datetime.now(timezone.utc) - timedelta(seconds=400)
    n = mp_tasks.sweep_expired(ttl_seconds=300)
    assert n == 1
    assert mp_tasks.get_task(task.task_id) is None

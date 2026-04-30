"""Tests for sembr.dashboard.read_model.build_snapshot and related helpers.

Cover (per test plan):
  (a) snapshot in empty DB / 0 feed must not raise
  (b) sparkline buckets length == 24
  (c) consecutive_failures correctness (3 fail + 1 ok → 0; 4 fail → 4)
  (d) pending/dead counts read straight from articles tables
  (e) qdrant ping/count/scroll mocked at boundary
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.dashboard.events import init_event_log_tables
from sembr.dashboard.read_model import build_snapshot
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


async def _setup(tmp_path):
    db_path = str(tmp_path / "sembr.db")
    conn = await init_sqlite(db_path)
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)
    return conn


def _qdrant_handle(*, ping: bool = True, count: int = 100):
    h = MagicMock()
    h.ping = AsyncMock(return_value=ping)
    h.client.count = AsyncMock(return_value=MagicMock(count=count))
    return h


def _embedder(status: str = "ok") -> MagicMock:
    e = MagicMock()
    e.status = status
    e.model_version = "bge-m3_v1"
    return e


def test_build_snapshot_empty_db_does_not_raise(tmp_path):
    async def run():
        await _setup(tmp_path)
        snap = await build_snapshot(get_conn(), _qdrant_handle(count=0), _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.schema_version == 1
    assert snap.feeds == []
    assert snap.articles.pending_count == 0
    assert snap.articles.dead_count == 0
    assert snap.articles.qdrant_count == 0
    assert snap.embedder.calls_24h.total == 0
    assert len(snap.embedder.calls_24h.sparkline_latency_ms) == 24


def test_build_snapshot_feed_sparkline_has_24_buckets(tmp_path):
    async def run():
        conn = await _setup(tmp_path)
        await conn.execute(
            "INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://x')"
        )
        # one ok event 1h ago
        ts = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        await conn.execute(
            "INSERT INTO feed_fetch_log "
            "(feed_id, started_at, elapsed_ms, ok, items_seen, items_new, "
            " error_class, error_message) "
            "VALUES (1, ?, 100, 1, 5, 2, NULL, NULL)",
            (ts,),
        )
        await conn.commit()
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert len(snap.feeds) == 1
    assert len(snap.feeds[0].fetch_24h.sparkline_buckets) == 24
    assert sum(snap.feeds[0].fetch_24h.sparkline_buckets) == 1
    assert snap.feeds[0].fetch_24h.last_outcome == "ok"


def test_build_snapshot_consecutive_failures_three_fail_then_ok_resets(tmp_path):
    async def run():
        conn = await _setup(tmp_path)
        await conn.execute(
            "INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://x')"
        )
        # Insert oldest first; row id is monotonic so DESC(id) == newest-first.
        # Order from oldest → newest: fail, fail, fail, ok. Latest is ok ⇒ streak=0.
        now = datetime.now(timezone.utc)
        for i, ok in enumerate([0, 0, 0, 1]):
            ts = (now - timedelta(minutes=10 * (4 - i))).isoformat()
            err = ("FetchError", "boom") if ok == 0 else (None, None)
            await conn.execute(
                "INSERT INTO feed_fetch_log "
                "(feed_id, started_at, elapsed_ms, ok, items_seen, items_new, "
                " error_class, error_message) "
                "VALUES (1, ?, 1, ?, 0, 0, ?, ?)",
                (ts, ok, err[0], err[1]),
            )
        await conn.commit()
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    fb = snap.feeds[0].fetch_24h
    assert fb.total == 4
    assert fb.fail == 3
    assert fb.ok == 1
    assert fb.last_outcome == "ok"
    assert fb.consecutive_failures == 0
    assert fb.last_error_message is None


def test_build_snapshot_consecutive_failures_four_fail(tmp_path):
    async def run():
        conn = await _setup(tmp_path)
        await conn.execute(
            "INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://x')"
        )
        now = datetime.now(timezone.utc)
        for i in range(4):
            ts = (now - timedelta(minutes=10 * (4 - i))).isoformat()
            await conn.execute(
                "INSERT INTO feed_fetch_log "
                "(feed_id, started_at, elapsed_ms, ok, items_seen, items_new, "
                " error_class, error_message) "
                "VALUES (1, ?, 1, 0, 0, 0, 'FetchError', 'boom')",
                (ts,),
            )
        await conn.commit()
        snap = await build_snapshot(get_conn(), _qdrant_handle(), _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    fb = snap.feeds[0].fetch_24h
    assert fb.total == 4
    assert fb.consecutive_failures == 4
    assert fb.last_outcome == "fail"
    assert fb.last_error_message == "boom"


def test_build_snapshot_articles_counts(tmp_path):
    async def run():
        conn = await _setup(tmp_path)
        await conn.execute(
            "INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://x')"
        )
        for i in range(3):
            await conn.execute(
                "INSERT INTO pending_articles "
                "(md5, feed_id, url, title, body) VALUES (?, 1, 'u', 't', 'b')",
                (f"{i:032x}",),
            )
        for i in range(2):
            await conn.execute(
                "INSERT INTO dead_articles "
                "(md5, feed_id, url, title, body, error_message) "
                "VALUES (?, 1, 'u', 't', 'b', 'oops')",
                (f"d{i:031x}",),
            )
        await conn.commit()
        snap = await build_snapshot(
            get_conn(), _qdrant_handle(count=42), _embedder()
        )
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.articles.pending_count == 3
    assert snap.articles.dead_count == 2
    assert snap.articles.qdrant_count == 42


def test_build_snapshot_components_when_qdrant_down(tmp_path):
    async def run():
        await _setup(tmp_path)
        snap = await build_snapshot(
            get_conn(), _qdrant_handle(ping=False, count=0), _embedder("error")
        )
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.components.qdrant == "down"
    assert snap.components.sqlite == "ok"
    assert snap.components.embedder == "error"


def test_build_snapshot_when_qdrant_handle_none(tmp_path):
    """Lifespan probe arrives before app.state.qdrant is assigned — must degrade,
    not crash, returning components.qdrant == 'down' and qdrant_count == 0."""
    async def run():
        await _setup(tmp_path)
        snap = await build_snapshot(get_conn(), None, _embedder())
        await close_sqlite()
        return snap

    snap = asyncio.run(run())
    assert snap.components.qdrant == "down"
    assert snap.articles.qdrant_count == 0

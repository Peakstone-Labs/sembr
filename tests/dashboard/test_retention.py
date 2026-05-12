"""Tests for sembr.dashboard.retention._prune_logs.

Cover:
  - rows older than retention_days deleted
  - per-feed FIFO cap enforced (keep newest N per feed_id)
  - both feed_fetch_log and embed_call_log respect age cap
  - prune does not raise even if SQLite errors (best-effort)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from sembr.dashboard import retention
from sembr.dashboard.events import init_event_log_tables
from sembr.dashboard.retention import _prune_logs
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


def _settings(*, retention_days: int = 7, max_per_feed: int = 1000) -> MagicMock:
    s = MagicMock()
    s.dashboard_log_retention_days = retention_days
    s.dashboard_log_max_per_feed = max_per_feed
    return s


async def _seed_feeds(conn, ids: list[int]) -> None:
    for fid in ids:
        await conn.execute(
            "INSERT INTO feeds (id, name, url) VALUES (?, ?, ?)",
            (fid, f"f{fid}", f"http://example.com/{fid}"),
        )
    await conn.commit()


async def _insert_fetch_log(
    conn,
    feed_id: int,
    started_at: datetime,
    *,
    ok: bool = True,
) -> None:
    await conn.execute(
        "INSERT INTO feed_fetch_log "
        "(feed_id, started_at, elapsed_ms, ok, items_seen, items_new, "
        " error_class, error_message) "
        "VALUES (?, ?, 1, ?, 0, 0, NULL, NULL)",
        (feed_id, started_at.isoformat(), 1 if ok else 0),
    )


async def _insert_embed_log(conn, started_at: datetime) -> None:
    await conn.execute(
        "INSERT INTO embed_call_log "
        "(started_at, elapsed_ms, ok, batch_size, total_chars, timeout_seconds, "
        " error_class, error_message) "
        "VALUES (?, 1, 1, 1, 1, 30.0, NULL, NULL)",
        (started_at.isoformat(),),
    )


def test_prune_drops_rows_older_than_retention(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        conn = await init_sqlite(db_path)
        await init_feed_tables(conn)
        await init_event_log_tables(conn)
        await _seed_feeds(conn, [1])

        now = datetime.now(timezone.utc)
        old = now - timedelta(days=10)
        recent = now - timedelta(hours=1)
        await _insert_fetch_log(conn, 1, old)
        await _insert_fetch_log(conn, 1, recent)
        await _insert_embed_log(conn, old)
        await _insert_embed_log(conn, recent)
        await conn.commit()

        await _prune_logs(_settings(retention_days=7, max_per_feed=1000))

        async with conn.execute("SELECT COUNT(*) FROM feed_fetch_log") as cur:
            fetch_count = (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM embed_call_log") as cur:
            embed_count = (await cur.fetchone())[0]
        await close_sqlite()
        return fetch_count, embed_count

    fetch_count, embed_count = asyncio.run(run())
    assert fetch_count == 1
    assert embed_count == 1


def test_prune_enforces_per_feed_max(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        conn = await init_sqlite(db_path)
        await init_feed_tables(conn)
        await init_event_log_tables(conn)
        await _seed_feeds(conn, [1, 2])

        recent = datetime.now(timezone.utc) - timedelta(minutes=5)
        # 1500 rows for feed 1, 50 rows for feed 2
        for _ in range(1500):
            await _insert_fetch_log(conn, 1, recent)
        for _ in range(50):
            await _insert_fetch_log(conn, 2, recent)
        await conn.commit()

        await _prune_logs(_settings(retention_days=7, max_per_feed=1000))

        async with conn.execute(
            "SELECT feed_id, COUNT(*) FROM feed_fetch_log GROUP BY feed_id ORDER BY feed_id"
        ) as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    counts = dict(rows)
    assert counts[1] == 1000  # capped
    assert counts[2] == 50  # untouched (under cap)


def test_prune_swallows_db_errors(tmp_path):
    """The retention job is best-effort — _prune_logs must never raise out
    to APScheduler, otherwise the job would be evicted."""

    async def run():
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def boom_tx():
            raise RuntimeError("db unavailable")
            yield  # noqa: unreachable

        with patch.object(retention, "transaction", boom_tx):
            await _prune_logs(_settings())  # must not raise

    asyncio.run(run())  # passes iff no exception bubbles

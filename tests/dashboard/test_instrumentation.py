# SPDX-License-Identifier: Apache-2.0
"""Verify collect_feed and embedder_worker write feed_fetch_log / embed_call_log
rows on each exit branch.

Strategy:
  - Real aiosqlite + real init_event_log_tables (the events helper writes rows)
  - Mock the source / embedder / qdrant client at module boundaries so the test
    stays Windows-runnable without qdrant_client / SiliconFlow.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from sembr.collector.rss import FetchError
from sembr.collector import scheduler as collector_scheduler
from sembr.collector.scheduler import collect_feed
from sembr.dashboard.events import init_event_log_tables
from sembr.db.articles import PendingRow, init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite
from sembr.embedder import scheduler as embedder_scheduler
from sembr.embedder.scheduler import embedder_worker


async def _setup_db(tmp_path) -> None:
    db_path = str(tmp_path / "sembr.db")
    conn = await init_sqlite(db_path)
    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)
    await conn.execute(
        "INSERT INTO feeds (id, name, url) VALUES (1, 'feed', 'http://example.com/rss')"
    )
    await conn.commit()


def _fake_source(articles=None, exc: Exception | None = None) -> MagicMock:
    src_cls = MagicMock()
    inst = MagicMock()
    if exc is not None:
        inst.fetch = AsyncMock(side_effect=exc)
    else:
        inst.fetch = AsyncMock(return_value=articles or [])
    src_cls.return_value = inst
    return src_cls


# ---------------------------------------------------------------------------
# collect_feed exit branches
# ---------------------------------------------------------------------------


def test_collect_feed_writes_fetch_event_on_fetcherror(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        with patch.dict(
            collector_scheduler.SOURCE_REGISTRY,
            {"rss": _fake_source(exc=FetchError("boom"))},
        ):
            await collect_feed(1, "feed", "http://example.com/rss", "rss", {})
        conn = get_conn()
        async with conn.execute("SELECT ok, error_class, error_message FROM feed_fetch_log") as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, error_class, error_message = rows[0]
    assert ok == 0
    assert error_class == "FetchError"
    assert "boom" in error_message


def test_collect_feed_writes_fetch_event_on_generic_exception(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        with patch.dict(
            collector_scheduler.SOURCE_REGISTRY,
            {"rss": _fake_source(exc=RuntimeError("explode"))},
        ):
            await collect_feed(1, "feed", "http://example.com/rss", "rss", {})
        conn = get_conn()
        async with conn.execute("SELECT ok, error_class FROM feed_fetch_log") as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, error_class = rows[0]
    assert ok == 0
    assert error_class == "RuntimeError"


def test_collect_feed_writes_fetch_event_on_success_empty(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        with patch.dict(
            collector_scheduler.SOURCE_REGISTRY,
            {"rss": _fake_source(articles=[])},
        ):
            await collect_feed(1, "feed", "http://example.com/rss", "rss", {})
        conn = get_conn()
        async with conn.execute(
            "SELECT ok, items_seen, items_new, error_class FROM feed_fetch_log"
        ) as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, items_seen, items_new, error_class = rows[0]
    assert ok == 1
    assert items_seen == 0
    assert items_new == 0
    assert error_class is None


def test_collect_feed_unknown_source_type_writes_no_event(tmp_path):
    """Unknown source_type is a config error, not a fetch attempt — no row."""

    async def run():
        await _setup_db(tmp_path)
        await collect_feed(1, "feed", "http://example.com/rss", "nonsense", {})
        conn = get_conn()
        async with conn.execute("SELECT COUNT(*) FROM feed_fetch_log") as cur:
            count = (await cur.fetchone())[0]
        await close_sqlite()
        return count

    assert asyncio.run(run()) == 0


# ---------------------------------------------------------------------------
# embedder_worker exit branches
# ---------------------------------------------------------------------------


def _embedder(*, is_loaded: bool = True, exc: Exception | None = None) -> MagicMock:
    e = MagicMock()
    e.is_loaded = is_loaded
    e.model_version = "bge-m3_v1"
    e.max_input_chars = 8_000
    if exc is not None:
        e.aembed = AsyncMock(side_effect=exc)
    else:
        e.aembed = AsyncMock(return_value=[[0.1] * 1024])
    return e


def _qdrant(*, exc: Exception | None = None) -> MagicMock:
    q = MagicMock()
    if exc is not None:
        q.client.upsert = AsyncMock(side_effect=exc)
    else:
        q.client.upsert = AsyncMock(return_value=None)
    return q


async def _seed_pending(md5: str = "a" * 32) -> None:
    conn = get_conn()
    await conn.execute(
        "INSERT INTO pending_articles "
        "(md5, feed_id, url, title, body, published_at, retry_count) "
        "VALUES (?, 1, 'http://x/1', 'title', 'body', NULL, 0)",
        (md5,),
    )
    await conn.commit()


def test_embedder_worker_writes_event_on_embed_failure(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await _seed_pending()
        await embedder_worker(_embedder(exc=ValueError("siliconflow 500")), _qdrant())
        conn = get_conn()
        async with conn.execute("SELECT ok, error_class, error_message FROM embed_call_log") as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, error_class, error_message = rows[0]
    assert ok == 0
    assert error_class == "ValueError"
    assert "siliconflow 500" in error_message


def test_embedder_worker_writes_event_on_qdrant_transient(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await _seed_pending()
        await embedder_worker(
            _embedder(),
            _qdrant(exc=httpx.ConnectError("conn refused")),
        )
        conn = get_conn()
        async with conn.execute("SELECT ok, error_class FROM embed_call_log") as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, error_class = rows[0]
    assert ok == 0
    assert error_class == "qdrant_transient"


def test_embedder_worker_writes_event_on_qdrant_other_error(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await _seed_pending()
        await embedder_worker(
            _embedder(),
            _qdrant(exc=RuntimeError("collection missing")),
        )
        conn = get_conn()
        async with conn.execute("SELECT ok, error_class FROM embed_call_log") as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, error_class = rows[0]
    assert ok == 0
    assert error_class == "qdrant_error"


def test_embedder_worker_writes_event_on_success(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await _seed_pending()
        await embedder_worker(_embedder(), _qdrant())
        conn = get_conn()
        async with conn.execute(
            "SELECT ok, batch_size, total_chars, error_class FROM embed_call_log"
        ) as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return rows

    rows = asyncio.run(run())
    assert len(rows) == 1
    ok, batch_size, total_chars, error_class = rows[0]
    assert ok == 1
    assert batch_size == 1
    assert total_chars > 0
    assert error_class is None


def test_embedder_worker_no_event_on_unloaded(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await _seed_pending()
        await embedder_worker(_embedder(is_loaded=False), _qdrant())
        conn = get_conn()
        async with conn.execute("SELECT COUNT(*) FROM embed_call_log") as cur:
            count = (await cur.fetchone())[0]
        await close_sqlite()
        return count

    assert asyncio.run(run()) == 0


def test_embedder_worker_no_event_on_empty_batch(tmp_path):
    async def run():
        await _setup_db(tmp_path)
        await embedder_worker(_embedder(), _qdrant())
        conn = get_conn()
        async with conn.execute("SELECT COUNT(*) FROM embed_call_log") as cur:
            count = (await cur.fetchone())[0]
        await close_sqlite()
        return count

    assert asyncio.run(run()) == 0

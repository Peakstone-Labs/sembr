# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr.dashboard.events.

Cover:
  (a) DDL idempotent (run twice, no error, indices present)
  (b) error_message > 500 chars truncated
  (c) ok=True / ok=False stored as 1 / 0
  (d) write failure surfaces to caller (caller wraps in try/except)
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from sembr.dashboard import events
from sembr.dashboard.events import (
    init_event_log_tables,
    log_embed_event,
    log_fetch_event,
)
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, get_conn, init_sqlite


async def _setup_db(path: str) -> None:
    conn = await init_sqlite(path)
    await init_feed_tables(conn)
    await init_event_log_tables(conn)
    # one feed row so feed_id FK has a target
    await conn.execute("INSERT INTO feeds (id, name, url) VALUES (1, 'f', 'http://example.com')")
    await conn.commit()


def test_init_event_log_tables_is_idempotent(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        conn = await init_sqlite(db_path)
        await init_feed_tables(conn)
        await init_event_log_tables(conn)
        await init_event_log_tables(conn)  # second invocation must not raise
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_feed_fetch_log_%'"
        ) as cur:
            rows = await cur.fetchall()
        await close_sqlite()
        return [r[0] for r in rows]

    indexes = asyncio.run(run())
    assert "idx_feed_fetch_log_feed_started" in indexes
    assert "idx_feed_fetch_log_started" in indexes


def test_log_fetch_event_truncates_error_message(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        await _setup_db(db_path)
        long_msg = "x" * 1500
        await log_fetch_event(
            feed_id=1,
            started_at=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
            elapsed_ms=120,
            ok=False,
            items_seen=0,
            items_new=0,
            error_class="FetchError",
            error_message=long_msg,
        )
        conn = get_conn()
        async with conn.execute(
            "SELECT ok, error_class, length(error_message) FROM feed_fetch_log"
        ) as cur:
            row = await cur.fetchone()
        await close_sqlite()
        return row

    ok, error_class, msg_len = asyncio.run(run())
    assert ok == 0
    assert error_class == "FetchError"
    assert msg_len == 500


def test_log_fetch_event_success_row(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        await _setup_db(db_path)
        await log_fetch_event(
            feed_id=1,
            started_at=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
            elapsed_ms=345,
            ok=True,
            items_seen=10,
            items_new=4,
            error_class=None,
            error_message=None,
        )
        conn = get_conn()
        async with conn.execute(
            "SELECT ok, items_seen, items_new, error_class, error_message FROM feed_fetch_log"
        ) as cur:
            row = await cur.fetchone()
        await close_sqlite()
        return row

    ok, items_seen, items_new, error_class, error_message = asyncio.run(run())
    assert ok == 1
    assert items_seen == 10
    assert items_new == 4
    assert error_class is None
    assert error_message is None


def test_log_embed_event_writes_row(tmp_path):
    db_path = str(tmp_path / "sembr.db")

    async def run():
        await _setup_db(db_path)
        await log_embed_event(
            started_at=datetime(2026, 4, 30, 12, 0, tzinfo=UTC),
            elapsed_ms=1450,
            ok=True,
            batch_size=32,
            total_chars=24000,
            timeout_seconds=60.0,
            error_class=None,
            error_message=None,
        )
        conn = get_conn()
        async with conn.execute(
            "SELECT ok, batch_size, total_chars, timeout_seconds FROM embed_call_log"
        ) as cur:
            row = await cur.fetchone()
        await close_sqlite()
        return row

    ok, batch_size, total_chars, timeout_seconds = asyncio.run(run())
    assert ok == 1
    assert batch_size == 32
    assert total_chars == 24000
    assert timeout_seconds == 60.0


def test_log_event_propagates_db_failure_to_caller(tmp_path):
    """log_*_event runs inside an independent transaction(); if that raises,
    the exception MUST bubble to the caller — the dev contract is that the
    *caller* wraps the call in try/except and only logger.warning. We verify
    the helper itself does not silently swallow."""

    class Boom(Exception):
        pass

    async def run():
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def fake_tx():
            raise Boom("db down")
            yield  # noqa: unreachable

        with patch.object(events, "transaction", fake_tx):
            with pytest.raises(Boom):
                await log_fetch_event(
                    feed_id=1,
                    started_at=datetime(2026, 4, 30, tzinfo=UTC),
                    elapsed_ms=1,
                    ok=True,
                    items_seen=0,
                    items_new=0,
                    error_class=None,
                    error_message=None,
                )

    asyncio.run(run())

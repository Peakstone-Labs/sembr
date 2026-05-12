# SPDX-License-Identifier: Apache-2.0
"""Reconcile S10: insert_article_pending must emit an INFO log when an article
body exceeds the 1MB cap and gets truncated.

Targets the existing log site at sembr/db/articles.py:106-110 — design D8
explicitly avoids new code here, only the missing assertion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import aiosqlite
import pytest

from sembr.collector.base import RawArticle
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import _BODY_CAP_BYTES, init_article_tables, insert_article_pending
from sembr.db.feeds import init_feed_tables


async def _make_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = asyncio.Lock()
    return conn


async def _seed_feed(conn) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) "
        "VALUES ('T', 'http://truncate.example', 30)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
        return (await cur.fetchone())[0]


def _make_article(md5: str, body: str) -> RawArticle:
    return RawArticle(
        url="https://example.com/big",
        title="Big body article",
        body=body,
        content_quality="summary",
        published_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        feed_md5=md5,
    )


@pytest.mark.asyncio
async def test_body_truncate_emits_info_log_with_required_fields(caplog):
    conn = await _make_conn()
    feed_id = await _seed_feed(conn)
    big_body = "x" * (_BODY_CAP_BYTES + 17)
    md5 = "a" * 32
    article = _make_article(md5, big_body)

    with caplog.at_level("INFO", logger="sembr.db.articles"):
        result = await insert_article_pending(conn, article, feed_id)
    assert result is True

    # Find the truncation log line and assert all four required fields are present.
    matches = [r for r in caplog.records if "article body truncated" in r.getMessage()]
    assert matches, "expected a body-truncate INFO log line, got none"
    msg = matches[0].getMessage()
    assert f"feed_id={feed_id}" in msg
    assert f"md5={md5}" in msg
    assert f"original={len(big_body)}" in msg
    assert f"cap={_BODY_CAP_BYTES}" in msg

    # Verify the row actually got the truncated body, not the full one.
    async with conn.execute("SELECT length(body) FROM pending_articles WHERE md5=?", (md5,)) as cur:
        stored_len = (await cur.fetchone())[0]
    assert stored_len == _BODY_CAP_BYTES

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


@pytest.mark.asyncio
async def test_body_at_or_below_cap_emits_no_truncate_log(caplog):
    conn = await _make_conn()
    feed_id = await _seed_feed(conn)
    body = "y" * _BODY_CAP_BYTES  # exactly at cap, not over
    md5 = "b" * 32

    with caplog.at_level("INFO", logger="sembr.db.articles"):
        await insert_article_pending(conn, _make_article(md5, body), feed_id)

    assert not any("article body truncated" in r.getMessage() for r in caplog.records)

    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None

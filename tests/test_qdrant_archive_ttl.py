# SPDX-License-Identifier: Apache-2.0
"""Archive-enabled TTL pipeline tests (retrieve → enrich → upsert → delete →
cascade), including the per-stage abort semantics that implement the
no-data-loss invariant: a point may be deleted from news_current only after
Qdrant acknowledged its archive upsert.

The legacy pure-delete path keeps its own suite in
``test_maintenance_qdrant_ttl.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

import sembr.maintenance.qdrant_ttl as qdrant_ttl_mod
from sembr.config import Settings
from sembr.db import sqlite as _sqlite_mod
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.maintenance.qdrant_ttl import _run_qdrant_ttl
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


async def _close_conn(conn: aiosqlite.Connection) -> None:
    await conn.close()
    _sqlite_mod._conn = None
    _sqlite_mod._WRITE_LOCK = None


async def _seed_base(conn, md5s: list[str]) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t', 30)"
    )
    for m in md5s:
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, 1)", (m,))
    await conn.commit()
    return 1


async def _seed_intent(conn, intent_id: int) -> None:
    await conn.execute(
        "INSERT INTO intents (id, name, text, threshold, schedule, channels, enabled) "
        "VALUES (?, 'i', 't', 0.75, '{\"mode\":\"event\"}', '[]', 1)",
        (intent_id,),
    )
    await conn.commit()


def _payload(**overrides) -> dict:
    base = {
        "url": "https://www.reuters.com/world/x",
        "title": "Vodafone ups guidance",
        "body": "Vodafone raised its outlook for the year",
        "published_at": "2026-07-27T06:49:53+00:00",
        "feed_id": 1,
        "embedding_model_version": "bge-m3_v1",
        "ingested_at_ts": 1785000000,
    }
    base.update(overrides)
    return base


def _retrieved_point(uuid: str, *, vector=None, payload=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid,
        payload=payload if payload is not None else _payload(),
        vector=vector if vector is not None else [0.1] * 4,
    )


def _mk_handle(scroll_uuids: list[str], retrieved: list) -> MagicMock:
    scroll_points = []
    for u in scroll_uuids:
        p = MagicMock()
        p.id = u
        scroll_points.append(p)

    handle = MagicMock()
    handle.client.scroll = AsyncMock(return_value=(scroll_points, None))
    handle.client.retrieve = AsyncMock(return_value=retrieved)
    handle.client.delete = AsyncMock()
    handle.client.upsert = AsyncMock()
    return handle


@pytest.mark.asyncio
async def test_archive_ttl_happy_path_conservation(caplog):
    """All expired points archived (enriched, matched_intents captured before
    the cascade wipes match_seen), then deleted and cascaded — archived ==
    deleted_qdrant in the summary line."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(3)]
        await _seed_base(conn, md5s)
        await _seed_intent(conn, 1)
        await _seed_intent(conn, 2)
        uuids = [md5_to_uuid(m) for m in md5s]
        for iid in (1, 2):
            await conn.execute(
                "INSERT INTO match_seen (intent_id, article_id) VALUES (?, ?)",
                (iid, uuids[0]),
            )
        await conn.commit()

        handle = _mk_handle(uuids, [_retrieved_point(u) for u in uuids])

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        upsert_kwargs = handle.client.upsert.call_args.kwargs
        assert upsert_kwargs["collection_name"] == "news_archive"
        assert upsert_kwargs["wait"] is True
        points = upsert_kwargs["points"]
        assert len(points) == 3
        by_id = {p.id: p.payload for p in points}
        # match_seen history survives only inside the archived payload.
        assert sorted(by_id[uuids[0]]["matched_intents"]) == [1, 2]
        assert by_id[uuids[1]]["matched_intents"] == []
        for payload in by_id.values():
            assert payload["body_len"] > 0
            assert payload["lang"] == "en"
            assert payload["url_domain"] == "reuters.com"
            assert "published_at_ts" in payload
            assert "archived_at_ts" in payload

        handle.client.delete.assert_awaited()
        async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
            assert (await cur.fetchone())[0] == 0
        async with conn.execute("SELECT COUNT(*) FROM match_seen") as cur:
            assert (await cur.fetchone())[0] == 0

        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "archived=3" in summary
        assert "deleted_qdrant=3" in summary
        assert "aborted_stage=none" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_upsert_failure_aborts(caplog, monkeypatch):
    """Upsert failure must abort BEFORE any delete — and later batches must
    not run either."""
    conn = await _make_conn()
    try:
        monkeypatch.setattr(qdrant_ttl_mod, "_ARCHIVE_BATCH", 2)
        md5s = [f"{i:032x}" for i in range(4)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        handle = _mk_handle(uuids, [_retrieved_point(u) for u in uuids[:2]])
        handle.client.upsert = AsyncMock(side_effect=RuntimeError("qdrant down"))

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        handle.client.delete.assert_not_called()
        # Only the first batch's retrieve ran; the loop broke before batch 2.
        assert handle.client.retrieve.await_count == 1
        async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
            assert (await cur.fetchone())[0] == 4
        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "aborted_stage=upsert" in summary
        assert "archived=0" in summary
        assert "deleted_qdrant=0" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_delete_failure_no_cascade(caplog):
    """Delete failure after a successful upsert leaves the batch in both
    collections (benign) and must not cascade SQLite."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(2)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        handle = _mk_handle(uuids, [_retrieved_point(u) for u in uuids])
        handle.client.delete = AsyncMock(side_effect=RuntimeError("delete failed"))

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        handle.client.upsert.assert_awaited()
        async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
            assert (await cur.fetchone())[0] == 2
        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "aborted_stage=delete" in summary
        assert "archived=2" in summary
        assert "deleted_qdrant=0" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_retrieve_failure_aborts(caplog):
    """A failing retrieve call aborts the whole run with nothing deleted —
    equivalent to an upsert failure."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(2)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        handle = _mk_handle(uuids, [])
        handle.client.retrieve = AsyncMock(side_effect=RuntimeError("timeout"))

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        handle.client.upsert.assert_not_called()
        handle.client.delete.assert_not_called()
        async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
            assert (await cur.fetchone())[0] == 2
        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "aborted_stage=retrieve" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_skipped_missing(caplog):
    """Ids the scroll saw but retrieve no longer finds are skipped (counted),
    not fatal; only retrieved points are archived, deleted and cascaded."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(3)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        # Third point vanished between scroll and retrieve (manual prune race).
        handle = _mk_handle(uuids, [_retrieved_point(u) for u in uuids[:2]])

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        assert len(handle.client.upsert.call_args.kwargs["points"]) == 2
        async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
            assert (await cur.fetchone())[0] == 1  # missing point's row untouched
        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "skipped_missing=1" in summary
        assert "archived=2" in summary
        assert "deleted_qdrant=2" in summary
        assert "aborted_stage=none" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_poisoned_point_skipped(caplog):
    """A point with no usable vector is left in news_current (not archived,
    not deleted) and only stalls itself, never the run."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(3)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        retrieved = [
            _retrieved_point(uuids[0]),
            SimpleNamespace(id=uuids[1], payload=_payload(), vector=None),
            _retrieved_point(uuids[2]),
        ]
        handle = _mk_handle(uuids, retrieved)

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        archived_ids = {p.id for p in handle.client.upsert.call_args.kwargs["points"]}
        assert archived_ids == {uuids[0], uuids[2]}
        async with conn.execute("SELECT md5 FROM feed_items") as cur:
            remaining = {r[0] for r in await cur.fetchall()}
        assert remaining == {md5s[1]}  # poisoned point's SQLite row survives
        assert any(
            "no usable vector" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records
        )
        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "poisoned_skipped=1" in summary
        assert "archived=2" in summary
        assert "deleted_qdrant=2" in summary
    finally:
        await _close_conn(conn)


@pytest.mark.asyncio
async def test_archive_ttl_cascade_failure_logs_summary(caplog, monkeypatch):
    """A cascade failure still emits the conservation summary line — the
    ledger must have no silent gap (reconcile owns the data-side cleanup)."""
    conn = await _make_conn()
    try:
        md5s = [f"{i:032x}" for i in range(2)]
        await _seed_base(conn, md5s)
        uuids = [md5_to_uuid(m) for m in md5s]

        async def boom(_uuids):
            raise RuntimeError("sqlite gone")

        monkeypatch.setattr(qdrant_ttl_mod, "_cascade_delete_sqlite", boom)
        handle = _mk_handle(uuids, [_retrieved_point(u) for u in uuids])

        with caplog.at_level("INFO", logger="sembr.maintenance.qdrant_ttl"):
            await _run_qdrant_ttl(handle, Settings())

        summary = next(
            r.getMessage() for r in caplog.records if "qdrant_ttl run:" in r.getMessage()
        )
        assert "aborted_stage=cascade" in summary
        assert "archived=2" in summary
        assert "deleted_qdrant=2" in summary
        assert "deleted_feed_items=0" in summary
    finally:
        await _close_conn(conn)

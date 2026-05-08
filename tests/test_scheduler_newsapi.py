"""D1 / D6 / D9 / D10: scheduler-side wiring for newsapi feeds.

Verifies:
* SOURCE_REGISTRY exposes 'newsapi' so /api/dashboard/sources/schemas works
* add_feed_job(feed) with source_type='newsapi' registers the master job
  (NEWSAPI_MASTER_JOB_ID) and NOT a per-feed feed_<id> job
* remove_feed_job is async-safe and drops the master when no enabled
  newsapi feeds remain
* maybe_drop_newsapi_master_job is conservative — keeps the master when
  any other enabled newsapi feed is present
"""
from __future__ import annotations

from datetime import datetime

import aiosqlite
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sembr.collector.scheduler import (
    NEWSAPI_MASTER_JOB_ID,
    SOURCE_REGISTRY,
    add_feed_job,
    ensure_newsapi_master_job,
    maybe_drop_newsapi_master_job,
    remove_feed_job,
)
from sembr.config import get_settings
from sembr.models import Feed


def _make_feed(*, fid: int, source_type: str, url: str, enabled: bool = True) -> Feed:
    return Feed(
        id=fid,
        name=f"feed-{fid}",
        url=url,
        source_type=source_type,
        config={},
        poll_interval_minutes=30,
        last_collected_at=None,
        created_at="2026-05-08T00:00:00Z",
        enabled=enabled,
        tags=[],
    )


async def _setup_inmem_db_with_feeds(rows: list[dict]) -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    from sembr.db.feeds import init_feed_tables
    await init_feed_tables(conn)
    for r in rows:
        await conn.execute(
            "INSERT INTO feeds (id, name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (r["id"], r.get("name", f"f{r['id']}"), r["url"],
             r.get("source_type", "newsapi"), r.get("last_collected_at"),
             int(r.get("enabled", 1))),
        )
    await conn.commit()
    return conn


# ---------------------------------------------------------------------------
# D1: SOURCE_REGISTRY
# ---------------------------------------------------------------------------


def test_source_registry_exposes_newsapi() -> None:
    assert "newsapi" in SOURCE_REGISTRY
    cls = SOURCE_REGISTRY["newsapi"]
    # Must implement BaseSource
    assert hasattr(cls, "fetch")
    assert hasattr(cls, "health")
    assert hasattr(cls, "config_schema")
    schema = cls.config_schema()
    assert schema == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# D10: add_feed_job branches by source_type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_feed_job_newsapi_routes_to_master(monkeypatch) -> None:
    """add_feed_job(newsapi feed) must register the singleton master and
    NOT a per-feed feed_<id> job."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        feed = _make_feed(fid=42, source_type="newsapi", url="reuters.com")
        await add_feed_job(sch, feed)
        ids = {j.id for j in sch.get_jobs()}
        assert NEWSAPI_MASTER_JOB_ID in ids
        assert "feed_42" not in ids
    finally:
        sch.shutdown(wait=False)


@pytest.mark.asyncio
async def test_add_feed_job_rss_keeps_per_feed_job() -> None:
    """RSS path must still register feed_<id>."""
    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        feed = _make_feed(fid=1, source_type="rss", url="http://example.com/rss")
        await add_feed_job(sch, feed)
        ids = {j.id for j in sch.get_jobs()}
        assert "feed_1" in ids
        assert NEWSAPI_MASTER_JOB_ID not in ids
    finally:
        sch.shutdown(wait=False)


# ---------------------------------------------------------------------------
# D10: remove_feed_job + maybe_drop_newsapi_master_job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remove_last_newsapi_feed_drops_master(monkeypatch) -> None:
    """When the last enabled newsapi feed is removed, the master job is dropped."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com"},
    ])
    from sembr.db.sqlite import install_for_test
    install_for_test(conn)

    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        await ensure_newsapi_master_job(sch, get_settings())
        assert sch.get_job(NEWSAPI_MASTER_JOB_ID) is not None

        # Simulate the row being deleted before remove_feed_job runs (matches
        # api/feeds.remove_feed sequencing: DB cascade then scheduler unlink)
        await conn.execute("DELETE FROM feeds WHERE id=1")
        await conn.commit()

        await remove_feed_job(sch, 1)
        assert sch.get_job(NEWSAPI_MASTER_JOB_ID) is None
    finally:
        sch.shutdown(wait=False)
        await conn.close()


@pytest.mark.asyncio
async def test_master_kept_when_other_newsapi_feeds_remain(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com"},
        {"id": 2, "url": "bbc.com"},
    ])
    from sembr.db.sqlite import install_for_test
    install_for_test(conn)

    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        await ensure_newsapi_master_job(sch, get_settings())
        # Delete just feed 1; feed 2 still enabled
        await conn.execute("DELETE FROM feeds WHERE id=1")
        await conn.commit()

        await remove_feed_job(sch, 1)
        assert sch.get_job(NEWSAPI_MASTER_JOB_ID) is not None
    finally:
        sch.shutdown(wait=False)
        await conn.close()


@pytest.mark.asyncio
async def test_master_dropped_when_only_disabled_newsapi_feeds_remain(monkeypatch) -> None:
    """maybe_drop checks `enabled=1`, not just row presence — disabled rows
    don't keep the master alive."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com", "enabled": 0},
        {"id": 2, "url": "bbc.com", "enabled": 0},
    ])
    from sembr.db.sqlite import install_for_test
    install_for_test(conn)

    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        await ensure_newsapi_master_job(sch, get_settings())
        await maybe_drop_newsapi_master_job(sch, conn)
        assert sch.get_job(NEWSAPI_MASTER_JOB_ID) is None
    finally:
        sch.shutdown(wait=False)
        await conn.close()


@pytest.mark.asyncio
async def test_remove_rss_feed_does_not_touch_master(monkeypatch) -> None:
    """RSS deletion path: feed_<id> removed, master left alone iff newsapi feeds exist."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com", "source_type": "newsapi"},
        {"id": 2, "url": "http://example.com/rss", "source_type": "rss"},
    ])
    from sembr.db.sqlite import install_for_test
    install_for_test(conn)

    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        # register both
        await ensure_newsapi_master_job(sch, get_settings())
        rss_feed = _make_feed(fid=2, source_type="rss", url="http://example.com/rss")
        await add_feed_job(sch, rss_feed)
        assert sch.get_job("feed_2") is not None

        await conn.execute("DELETE FROM feeds WHERE id=2")
        await conn.commit()
        await remove_feed_job(sch, 2)
        assert sch.get_job("feed_2") is None
        # master kept because feed 1 still present
        assert sch.get_job(NEWSAPI_MASTER_JOB_ID) is not None
    finally:
        sch.shutdown(wait=False)
        await conn.close()


# ---------------------------------------------------------------------------
# D9: ensure_newsapi_master_job is idempotent (replace_existing)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_master_job_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    get_settings.cache_clear()
    sch = AsyncIOScheduler(timezone="UTC")
    sch.start(paused=True)
    try:
        await ensure_newsapi_master_job(sch, get_settings())
        first = sch.get_job(NEWSAPI_MASTER_JOB_ID)
        await ensure_newsapi_master_job(sch, get_settings())
        second = sch.get_job(NEWSAPI_MASTER_JOB_ID)
        assert second is not None
        # Same id, no exception raised on second call
        assert second.id == first.id
        assert len([j for j in sch.get_jobs() if j.id == NEWSAPI_MASTER_JOB_ID]) == 1
    finally:
        sch.shutdown(wait=False)

# SPDX-License-Identifier: Apache-2.0
"""Unit tests for /api/dashboard/articles?bucket=qdrant&... filter.

Three concerns:
1. Filter params are stitched into a qdrant ``Filter(must=[...])`` with the
   exact field names + match shapes — payload-index mismatch silently
   degrades to full scan (feedback_qdrant_client).
2. ``bucket=pending`` / ``bucket=dead`` reject the qdrant-only params with
   422 instead of silently dropping them.
3. ``ingested_from`` / ``ingested_to`` map YYYY-MM-DD to a half-open
   ``[from, to+1d)`` ts range — bad date strings yield 422.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.dashboard.routes import router
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, init_sqlite


@pytest.fixture
def captured_scroll() -> dict:
    """Captures the filter argument the route passes into qdrant.scroll()."""
    return {"call_args": None}


@pytest.fixture
def app(tmp_path, captured_scroll: dict) -> FastAPI:
    fake_qclient = MagicMock()

    async def _scroll(**kwargs):
        captured_scroll["call_args"] = kwargs
        return ([], None)  # empty page, no next cursor

    fake_qclient.scroll = AsyncMock(side_effect=_scroll)
    fake_qdrant = SimpleNamespace(client=fake_qclient)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        conn = await init_sqlite(str(tmp_path / "sembr.db"))
        await init_feed_tables(conn)
        await init_article_tables(conn)
        app.state.qdrant = fake_qdrant
        try:
            yield
        finally:
            await close_sqlite()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(router)
    return app


def test_articles_qdrant_no_filter_passes_no_filter(app: FastAPI, captured_scroll: dict):
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=qdrant&limit=5")
    assert r.status_code == 200, r.text
    # When there's no filter, scroll_filter must be absent so qdrant doesn't
    # incur the index-intersect cost.
    assert "scroll_filter" not in captured_scroll["call_args"]


def test_articles_qdrant_full_filter_set_passed_through(app: FastAPI, captured_scroll: dict):
    """All four filter fields combine into a single must list."""
    with TestClient(app) as client:
        r = client.get(
            "/api/dashboard/articles"
            "?bucket=qdrant"
            "&ingested_from=2026-01-01"
            "&ingested_to=2026-01-31"
            "&feed_id=42"
            "&title_q=AAPL"
        )
    assert r.status_code == 200, r.text
    qfilter = captured_scroll["call_args"]["scroll_filter"]
    must_keys = sorted(c.key for c in qfilter.must)
    # Three FieldConditions: range, feed_id, title (range covers both ts bounds)
    assert must_keys == ["feed_id", "ingested_at_ts", "title"]

    # ingested_at_ts must use Range:
    # gte = 2026-01-01 00:00 UTC, lt = 2026-02-01 00:00 UTC (end-of-day +1d)
    range_cond = next(c for c in qfilter.must if c.key == "ingested_at_ts")
    expected_gte = 1767225600  # 2026-01-01 00:00 UTC
    expected_lt = 1769904000  # 2026-02-01 00:00 UTC
    assert range_cond.range.gte == expected_gte
    assert range_cond.range.lt == expected_lt

    # feed_id MatchValue
    feed_cond = next(c for c in qfilter.must if c.key == "feed_id")
    assert feed_cond.match.value == 42

    # title MatchText
    title_cond = next(c for c in qfilter.must if c.key == "title")
    assert getattr(title_cond.match, "text", None) == "AAPL"


def test_articles_pending_with_qdrant_filter_returns_422(app: FastAPI):
    """qdrant-only filter on a sqlite bucket = client bug; 422 surfaces it."""
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=pending&feed_id=1")
    assert r.status_code == 422
    assert "qdrant" in r.json()["detail"]


def test_articles_dead_with_title_q_returns_422(app: FastAPI):
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=dead&title_q=foo")
    assert r.status_code == 422


def test_articles_qdrant_invalid_date_returns_422(app: FastAPI):
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=qdrant&ingested_from=not-a-date")
    assert r.status_code == 422
    assert "invalid date" in r.json()["detail"]


def test_articles_qdrant_invalid_feed_id_returns_422(app: FastAPI):
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=qdrant&feed_id=not-an-int")
    assert r.status_code == 422


def test_articles_qdrant_paginates_via_start_from_and_must_not_has_id(tmp_path):
    """Regression: Qdrant scroll(order_by=...) returns next_page_offset=None
    even when more matching points exist. The loop must drive pagination per
    Qdrant docs: order_by.start_from = last seen ts AND a HasIdCondition in
    must_not listing every previously seen point id (server-side dedup).
    Without this, every result page silently caps at one Qdrant page (~64
    hits) — what made title_q=伊朗 look like ~24h of coverage when >700 hits
    over 10+ days actually existed.
    """
    fake_qclient = MagicMock()
    calls: list[dict] = []

    page1 = [
        SimpleNamespace(
            id=f"p{i}",
            payload={
                "ingested_at_ts": 1000 - i,
                "title": f"t{i}",
                "url": "u",
                "feed_id": 1,
                "published_at": None,
            },
        )
        for i in range(60)
    ]
    # Page 2: only points Qdrant would return after must_not has_id filter
    # excludes p0..p59 — i.e. ids the loop has not seen yet.
    page2 = [
        SimpleNamespace(
            id=f"q{i}",
            payload={
                "ingested_at_ts": 940 - i,
                "title": f"q{i}",
                "url": "u",
                "feed_id": 1,
                "published_at": None,
            },
        )
        for i in range(5)
    ]

    async def _scroll(**kwargs):
        calls.append(kwargs)
        order = kwargs.get("order_by") or {}
        start_from = order.get("start_from")
        if start_from is None:
            return (page1, None)
        return (page2, None)

    fake_qclient.scroll = AsyncMock(side_effect=_scroll)
    fake_qdrant = SimpleNamespace(client=fake_qclient)

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        conn = await init_sqlite(str(tmp_path / "sembr.db"))
        await init_feed_tables(conn)
        await init_article_tables(conn)
        app.state.qdrant = fake_qdrant
        try:
            yield
        finally:
            await close_sqlite()

    app = FastAPI(lifespan=_lifespan)
    app.include_router(router)

    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=qdrant&limit=63")
    assert r.status_code == 200, r.text
    rows = r.json()
    # 60 from page1 + 3 from page2 (limit=63 caps the run).
    assert len(rows) == 63
    md5s = [row["md5"] for row in rows]
    assert len(md5s) == len(set(md5s)), "duplicate point ids leaked through"

    # First call: no start_from, no scroll_filter (no base filter on this route).
    assert len(calls) == 2
    assert "start_from" not in calls[0]["order_by"]
    assert "scroll_filter" not in calls[0]

    # Second call: start_from = last seen ts (941, p59); scroll_filter has a
    # must_not entry that is a HasIdCondition listing all 60 page-1 ids.
    assert calls[1]["order_by"]["start_from"] == 941
    qfilter = calls[1]["scroll_filter"]
    assert qfilter.must_not is not None and len(qfilter.must_not) == 1
    has_id_cond = qfilter.must_not[0]
    excluded = set(getattr(has_id_cond, "has_id", []))
    assert excluded == {f"p{i}" for i in range(60)}

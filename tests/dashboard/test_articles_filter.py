"""Unit tests for /api/dashboard/articles?bucket=qdrant&... filter (D7).

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


def test_articles_qdrant_no_filter_passes_no_filter(
    app: FastAPI, captured_scroll: dict
):
    with TestClient(app) as client:
        r = client.get("/api/dashboard/articles?bucket=qdrant&limit=5")
    assert r.status_code == 200, r.text
    # When there's no filter, scroll_filter must be absent so qdrant doesn't
    # incur the index-intersect cost.
    assert "scroll_filter" not in captured_scroll["call_args"]


def test_articles_qdrant_full_filter_set_passed_through(
    app: FastAPI, captured_scroll: dict
):
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
    expected_lt = 1769904000   # 2026-02-01 00:00 UTC
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
        r = client.get(
            "/api/dashboard/articles?bucket=qdrant&ingested_from=not-a-date"
        )
    assert r.status_code == 422
    assert "invalid date" in r.json()["detail"]


def test_articles_qdrant_invalid_feed_id_returns_422(app: FastAPI):
    with TestClient(app) as client:
        r = client.get(
            "/api/dashboard/articles?bucket=qdrant&feed_id=not-an-int"
        )
    assert r.status_code == 422

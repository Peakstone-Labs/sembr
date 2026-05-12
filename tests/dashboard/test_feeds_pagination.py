"""SC#4: GET /api/dashboard/feeds returns the correct page slice + total."""

from __future__ import annotations

import os
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.feeds import router as feeds_router
from sembr.dashboard.routes import router as dashboard_router
from sembr.dashboard.events import init_event_log_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.sqlite import close_sqlite, init_sqlite


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await init_sqlite(path)
        await init_feed_tables(conn)
        await init_event_log_tables(conn)
        yield
        await close_sqlite()

    app = FastAPI(lifespan=lifespan)
    # mount feeds + dashboard routers; mock scheduler so add_feed_job no-ops
    app.include_router(feeds_router)
    app.include_router(dashboard_router)
    sched = MagicMock()
    sched.get_job = MagicMock(return_value=None)
    app.state.scheduler = sched
    app.state.qdrant = None

    from unittest.mock import AsyncMock, patch

    with patch("sembr.api.feeds.add_feed_job", new=AsyncMock()):
        with TestClient(app) as c:
            yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def _seed_feeds(client: TestClient, n: int) -> None:
    for i in range(n):
        r = client.post(
            "/feeds",
            json={
                "name": f"feed-{i:02d}",
                "url": f"https://example.com/feed-{i:02d}.rss",
                "tags": ["even"] if i % 2 == 0 else ["odd"],
            },
        )
        assert r.status_code == 201, r.text


def test_pagination_default_limit(client: TestClient) -> None:
    _seed_feeds(client, 25)
    r = client.get("/api/dashboard/feeds")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 25
    assert len(body["items"]) == 20  # default limit


def test_pagination_offset_returns_remainder(client: TestClient) -> None:
    _seed_feeds(client, 25)
    r = client.get("/api/dashboard/feeds?limit=20&offset=20")
    body = r.json()
    assert body["total"] == 25
    assert len(body["items"]) == 5


def test_filter_by_tag(client: TestClient) -> None:
    _seed_feeds(client, 10)
    r = client.get("/api/dashboard/feeds?tag=odd&limit=100")
    body = r.json()
    assert body["total"] == 5
    assert all("odd" in f["tags"] for f in body["items"])


def test_filter_by_q(client: TestClient) -> None:
    _seed_feeds(client, 10)
    r = client.get("/api/dashboard/feeds?q=feed-03")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "feed-03"


def test_each_item_has_group_key(client: TestClient) -> None:
    _seed_feeds(client, 3)
    r = client.get("/api/dashboard/feeds")
    body = r.json()
    for item in body["items"]:
        assert item["group_key"]  # non-empty
        assert "tags" in item


def test_limit_max_enforced(client: TestClient) -> None:
    r = client.get("/api/dashboard/feeds?limit=999")
    assert r.status_code == 422

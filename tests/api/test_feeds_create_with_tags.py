# SPDX-License-Identifier: Apache-2.0
"""SC#2 server-side: POST /feeds with tags and PATCH /feeds/{id}/tags.

Uses a real on-disk SQLite (init_sqlite + WAL) so transaction() works against
the module-global connection. The scheduler is mocked so no APScheduler runs.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.feeds import router as feeds_router
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.sqlite import close_sqlite, init_sqlite


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await init_sqlite(path)
        await init_feed_tables(conn)
        await init_intent_tables(conn)
        yield
        await close_sqlite()

    app = FastAPI(lifespan=lifespan)
    app.include_router(feeds_router)
    app.state.scheduler = MagicMock()

    with patch("sembr.api.feeds.add_feed_job", new=AsyncMock()):
        with TestClient(app) as c:
            yield c

    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path + suffix)


def test_post_feed_with_tags_201(client: TestClient) -> None:
    resp = client.post(
        "/feeds",
        json={
            "name": "My RSS",
            "url": "https://example.com/rss.xml",
            "tags": ["AI", "news", "ai"],  # case-insensitive dedup
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "My RSS"
    assert sorted(body["tags"]) == ["ai", "news"]


def test_post_feed_invalid_tag_422(client: TestClient) -> None:
    resp = client.post(
        "/feeds",
        json={
            "name": "Bad",
            "url": "https://example.com/x.rss",
            "tags": ["UPPER_CASE_BAD!"],  # contains _ and !
        },
    )
    assert resp.status_code == 422


def test_post_feed_too_many_tags_422(client: TestClient) -> None:
    resp = client.post(
        "/feeds",
        json={
            "name": "Many",
            "url": "https://example.com/y.rss",
            "tags": [f"t{i}" for i in range(11)],
        },
    )
    assert resp.status_code == 422


def test_get_feeds_includes_tags(client: TestClient) -> None:
    client.post(
        "/feeds",
        json={"name": "A", "url": "https://a.example.com/r.xml", "tags": ["a"]},
    )
    client.post(
        "/feeds",
        json={"name": "B", "url": "https://b.example.com/r.xml", "tags": ["b", "c"]},
    )
    resp = client.get("/feeds")
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    by_name = {f["name"]: f for f in items}
    assert by_name["A"]["tags"] == ["a"]
    assert sorted(by_name["B"]["tags"]) == ["b", "c"]


def test_patch_feed_tags_replaces(client: TestClient) -> None:
    create = client.post(
        "/feeds",
        json={"name": "P", "url": "https://p.example.com/r.xml", "tags": ["old"]},
    )
    feed_id = create.json()["id"]

    resp = client.patch(f"/feeds/{feed_id}/tags", json={"tags": ["new1", "new2"]})
    assert resp.status_code == 200, resp.text
    assert sorted(resp.json()["tags"]) == ["new1", "new2"]

    # Old tag must be gone (replace semantics, not merge).
    listing = client.get("/feeds").json()
    [feed] = [f for f in listing if f["id"] == feed_id]
    assert sorted(feed["tags"]) == ["new1", "new2"]


def test_patch_feed_tags_404(client: TestClient) -> None:
    resp = client.patch("/feeds/9999/tags", json={"tags": ["x"]})
    assert resp.status_code == 404


def test_patch_feed_tags_clear(client: TestClient) -> None:
    create = client.post(
        "/feeds",
        json={"name": "C", "url": "https://c.example.com/r.xml", "tags": ["a", "b"]},
    )
    feed_id = create.json()["id"]
    resp = client.patch(f"/feeds/{feed_id}/tags", json={"tags": []})
    assert resp.status_code == 200
    assert resp.json()["tags"] == []

"""Tests for POST /feeds/{id}/fire + GET .../fire/{task_id} (SC#6–9)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.feeds_fire import router as feeds_fire_router
from sembr.api.feeds import router as feeds_router
from sembr.collector.fire_tasks import _reset_for_testing
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.sqlite import close_sqlite, init_sqlite


@pytest.fixture(autouse=True)
def reset_fire_tasks():
    _reset_for_testing()
    yield
    _reset_for_testing()


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
    app.include_router(feeds_fire_router)
    app.state.scheduler = MagicMock()

    with patch("sembr.api.feeds.add_feed_job", new=AsyncMock()):
        with TestClient(app) as c:
            yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def _create_feed(client: TestClient, enabled: bool = True) -> dict:
    resp = client.post("/feeds", json={"name": "Test", "url": "https://fire.example.com/rss"})
    assert resp.status_code == 201, resp.text
    feed = resp.json()
    if not enabled:
        with patch("sembr.api.feeds.add_feed_job", new=AsyncMock()):
            with patch("sembr.api.feeds.remove_feed_job"):
                r = client.patch(f"/feeds/{feed['id']}", json={"enabled": False})
                assert r.status_code == 200
    return feed


# SC#6: dry_run does not write to any table
def test_feeds_fire_dry_run_no_writes(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    # Mock _feed_dry_run to complete synchronously
    async def fake_dry_run(task, feed_url, source_type, config, since):
        task.articles = [{"title": "t", "url": "u", "published_at": None, "status": "NEW"}]
        task.articles_fetched = 1
        task.articles_new = 1
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)

    with patch("sembr.api.feeds_fire._feed_dry_run", side_effect=fake_dry_run):
        resp = client.post(f"/feeds/{feed_id}/fire?dry_run=true")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert "task_id" in body

    # Poll until done
    task_id = body["task_id"]
    status_resp = client.get(f"/feeds/{feed_id}/fire/{task_id}")
    assert status_resp.status_code == 200
    result = status_resp.json()
    assert result["dry_run"] is True
    assert result["status"] == "done"


# SC#7: real run creates a non-dry-run task and returns 202
def test_feeds_fire_real_run_writes_log(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    async def instant_run(task, fid, fname, furl, stype, cfg):
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)

    with patch("sembr.api.feeds_fire._feed_real_run", side_effect=instant_run):
        resp = client.post(f"/feeds/{feed_id}/fire?dry_run=false")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    task_id = body["task_id"]

    # Verify task is in the store and is a real (non-dry) run
    from sembr.collector.fire_tasks import get_task as ft_get
    task = ft_get(task_id)
    assert task is not None
    assert task.dry_run is False
    assert task.feed_id == feed_id


# SC#8a: real run throttle — second request within 60s returns 429
def test_feeds_fire_real_run_throttle_429(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    async def instant_run(task, fid, fname, furl, stype, cfg):
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)

    with patch("sembr.api.feeds_fire._feed_real_run", side_effect=instant_run):
        r1 = client.post(f"/feeds/{feed_id}/fire?dry_run=false")
    assert r1.status_code == 202

    # Second fire immediately — should be rate-limited
    r2 = client.post(f"/feeds/{feed_id}/fire?dry_run=false")
    assert r2.status_code == 429
    assert "60 seconds" in r2.json()["detail"]


# SC#8b: dry_run does not consume rate limit (can be called many times)
def test_feeds_fire_dry_run_no_throttle(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    async def instant_dry(task, feed_url, source_type, config, since):
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)

    with patch("sembr.api.feeds_fire._feed_dry_run", side_effect=instant_dry):
        for _ in range(5):
            r = client.post(f"/feeds/{feed_id}/fire?dry_run=true")
            assert r.status_code == 202, f"Unexpected {r.status_code}: {r.text}"


# SC#9: disabled feed can still be fired (real run)
def test_feeds_fire_disabled_feed_real_run_works(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    # Disable the feed
    with patch("sembr.api.feeds.remove_feed_job"):
        client.patch(f"/feeds/{feed_id}", json={"enabled": False})

    async def instant_run(task, fid, fname, furl, stype, cfg):
        task.status = "done"
        task.finished_at = datetime.now(timezone.utc)

    with patch("sembr.api.feeds_fire._feed_real_run", side_effect=instant_run):
        resp = client.post(f"/feeds/{feed_id}/fire?dry_run=false")
    assert resp.status_code == 202, resp.text


# GET task 404 for nonexistent task_id
def test_feeds_fire_get_task_404(client: TestClient) -> None:
    feed = _create_feed(client)
    resp = client.get(f"/feeds/{feed['id']}/fire/nonexistent-task-id")
    assert resp.status_code == 404


# POST fire 404 for nonexistent feed
def test_feeds_fire_post_404(client: TestClient) -> None:
    resp = client.post("/feeds/9999/fire?dry_run=true")
    assert resp.status_code == 404

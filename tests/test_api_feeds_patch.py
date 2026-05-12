# SPDX-License-Identifier: Apache-2.0
"""Tests for PATCH /feeds/{id} — Toggle + Edit behaviors (SC#1–5)."""

from __future__ import annotations

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

    mock_add = AsyncMock()
    mock_remove = AsyncMock()
    with patch("sembr.api.feeds.add_feed_job", new=mock_add):
        with patch("sembr.api.feeds.remove_feed_job", new=mock_remove):
            with TestClient(app) as c:
                c._mock_add = mock_add
                c._mock_remove = mock_remove
                yield c

    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except FileNotFoundError:
            pass


def _create_feed(
    client: TestClient, name: str = "Test", url: str = "https://example.com/rss"
) -> dict:
    resp = client.post("/feeds", json={"name": name, "url": url})
    assert resp.status_code == 201, resp.text
    return resp.json()


# SC#1 + SC#2: toggle enabled/disabled triggers scheduler changes
def test_feeds_patch_enabled_toggles_job(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]
    assert feed["enabled"] is True

    # Toggle off
    resp = client.patch(f"/feeds/{feed_id}", json={"enabled": False})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    client._mock_remove.assert_called_once()

    # Toggle back on
    client._mock_add.reset_mock()
    resp = client.patch(f"/feeds/{feed_id}", json={"enabled": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    client._mock_add.assert_called_once()


# SC#3: disabled feed is still returned by GET /feeds
def test_feeds_patch_disabled_articles_still_visible(client: TestClient) -> None:
    feed = _create_feed(client, name="Disabled", url="https://disabled.example.com/rss")
    feed_id = feed["id"]

    resp = client.patch(f"/feeds/{feed_id}", json={"enabled": False})
    assert resp.status_code == 200

    listing = client.get("/feeds").json()
    found = [f for f in listing if f["id"] == feed_id]
    assert len(found) == 1
    assert found[0]["enabled"] is False


# SC#4: PATCH rejects url and source_type (extra="forbid")
def test_feeds_patch_rejects_url_and_source_type(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    resp = client.patch(f"/feeds/{feed_id}", json={"url": "https://evil.com/rss"})
    assert resp.status_code == 422, resp.text

    resp = client.patch(f"/feeds/{feed_id}", json={"source_type": "http"})
    assert resp.status_code == 422, resp.text


# SC#5: changing poll_interval reschedules when feed is enabled
def test_feeds_patch_poll_interval_reschedules(client: TestClient) -> None:
    feed = _create_feed(client)
    feed_id = feed["id"]

    client._mock_add.reset_mock()
    resp = client.patch(f"/feeds/{feed_id}", json={"poll_interval_minutes": 10})
    assert resp.status_code == 200, resp.text
    assert resp.json()["poll_interval_minutes"] == 10
    # add_feed_job called to reschedule with new interval
    client._mock_add.assert_called_once()


# PATCH 404 on nonexistent feed
def test_feeds_patch_404(client: TestClient) -> None:
    resp = client.patch("/feeds/9999", json={"name": "ghost"})
    assert resp.status_code == 404


# PATCH name and tags (no scheduler change)
def test_feeds_patch_name_and_tags(client: TestClient) -> None:
    feed = _create_feed(client, name="original", url="https://patch-name.example.com/rss")
    feed_id = feed["id"]

    client._mock_add.reset_mock()
    resp = client.patch(f"/feeds/{feed_id}", json={"name": "renamed", "tags": ["news"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "renamed"
    assert body["tags"] == ["news"]
    # No scheduler change for name-only edit
    client._mock_add.assert_not_called()
    client._mock_remove.assert_not_called()


# F16: no-op PATCH {"enabled": true} on already-enabled feed does NOT call add_feed_job
def test_feeds_patch_enabled_noop_no_reschedule(client: TestClient) -> None:
    feed = _create_feed(client, name="noop", url="https://noop.example.com/rss")
    feed_id = feed["id"]
    assert feed["enabled"] is True

    client._mock_add.reset_mock()
    resp = client.patch(f"/feeds/{feed_id}", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    # Value unchanged — scheduler must not be touched
    client._mock_add.assert_not_called()
    client._mock_remove.assert_not_called()


# PATCH disabled feed changing poll_interval does NOT reschedule
def test_feeds_patch_interval_disabled_no_reschedule(client: TestClient) -> None:
    feed = _create_feed(client, name="off", url="https://off.example.com/rss")
    feed_id = feed["id"]

    client.patch(f"/feeds/{feed_id}", json={"enabled": False})
    client._mock_add.reset_mock()
    client._mock_remove.reset_mock()

    resp = client.patch(f"/feeds/{feed_id}", json={"poll_interval_minutes": 60})
    assert resp.status_code == 200
    # disabled feed — no job added
    client._mock_add.assert_not_called()

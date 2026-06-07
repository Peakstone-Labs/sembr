# SPDX-License-Identifier: Apache-2.0
"""Regression tests for DELETE /feeds/{id} — feed delete + intent cascade.

Guards the bug where ``remove_feed`` ran ``intents_remove_feed_id`` (a bare
UPDATE that opens an *implicit* transaction under the connection's deferred
``isolation_level``) and then called ``delete_feed`` (which issues its own
``BEGIN``), tripping SQLite's "cannot start a transaction within a transaction".
Because that BEGIN raised inside ``transaction()``'s ``__aenter__`` — before its
self-healing ROLLBACK — the shared connection was left wedged in an open
transaction and EVERY later writer 500'd until the process restarted.

Uses a real on-disk SQLite db via ``init_sqlite`` so the deferred-isolation
behaviour matches production. The ``:memory:`` unit fixtures in
``test_db_cascade.py`` call ``intents_remove_feed_id`` then ``commit()`` in
isolation and so do NOT reproduce the combined-path bug.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
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

    # Scheduler/matcher side effects are out of scope here — mock them so the
    # tests exercise only the DB transaction path of remove_feed.
    with (
        patch("sembr.api.feeds.add_feed_job", new=AsyncMock()),
        patch("sembr.api.feeds.remove_feed_job", new=AsyncMock()),
        patch("sembr.api.feeds.reregister_intent_job", new=MagicMock()),
    ):
        with TestClient(app) as c:
            c._db_path = path  # noqa: SLF001 — test-only handle for direct asserts
            yield c

    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path + suffix)


def _create_feed(client: TestClient, name: str, url: str) -> dict:
    resp = client.post("/feeds", json={"name": name, "url": url})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_feeds_delete_returns_204_and_removes_row(client: TestClient) -> None:
    feed = _create_feed(client, "del", "https://del.example.com/rss")
    resp = client.delete(f"/feeds/{feed['id']}")
    assert resp.status_code == 204, resp.text
    assert all(f["id"] != feed["id"] for f in client.get("/feeds").json())


def test_feeds_delete_does_not_poison_connection(client: TestClient) -> None:
    """The core regression: a delete must leave the shared write connection
    usable. Pre-fix this DELETE 500'd ("cannot start a transaction within a
    transaction") AND wedged the connection so the follow-up create also 500'd.
    ``intents_remove_feed_id`` runs its UPDATE even with zero matching intents,
    so the bug reproduces with no intent present.
    """
    a = _create_feed(client, "a", "https://a.example.com/rss")
    assert client.delete(f"/feeds/{a['id']}").status_code == 204, "delete itself must succeed"

    # The assertion that fails pre-fix: writes still work after a delete.
    b = _create_feed(client, "b", "https://b.example.com/rss")
    assert b["id"] != a["id"]
    ids = {f["id"] for f in client.get("/feeds").json()}
    assert a["id"] not in ids
    assert b["id"] in ids


def test_feeds_delete_cascades_intent_filter_atomically(client: TestClient) -> None:
    """Deleting a feed scrubs its id from every intent feed_filter, in the same
    transaction as the feed-row removal."""
    feed = _create_feed(client, "watched", "https://watched.example.com/rss")
    fid = feed["id"]

    # Seed an intent referencing the feed via a direct sync connection — avoids
    # calling the aiosqlite singleton from this sync test's thread/loop. schedule
    # must be a valid Schedule JSON (remove_feed re-parses cascaded intents via
    # get_intent), so generate it from the model rather than the bare table
    # default '{}'.
    from sembr.models import CronSchedule  # noqa: PLC0415

    raw = sqlite3.connect(client._db_path)
    raw.execute(
        "INSERT INTO intents (name, text, channels, feed_filter, schedule) VALUES (?,?,?,?,?)",
        (
            "watch",
            "watch intent",
            json.dumps([{"type": "email", "to": ["a@example.com"]}]),
            json.dumps({"ids": [fid]}),
            CronSchedule(preset="daily").model_dump_json(),
        ),
    )
    raw.commit()
    raw.close()

    assert client.delete(f"/feeds/{fid}").status_code == 204

    raw = sqlite3.connect(client._db_path)
    (ff,) = raw.execute("SELECT feed_filter FROM intents WHERE name='watch'").fetchone()
    raw.close()
    assert json.loads(ff)["ids"] == []  # feed id scrubbed by the cascade


def test_feeds_delete_404(client: TestClient) -> None:
    assert client.delete("/feeds/9999").status_code == 404

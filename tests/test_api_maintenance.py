# SPDX-License-Identifier: Apache-2.0
"""Tests for POST/GET /api/dashboard/maintenance/* (S6 + S7 + S8)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.maintenance import router as maintenance_router
from sembr.db.articles import init_article_tables
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.db.sqlite import close_sqlite, init_sqlite
from sembr.maintenance import tasks as mp_tasks
from sembr.vector_store.news import md5_to_uuid


@pytest.fixture(autouse=True)
def _reset_tasks():
    mp_tasks._reset_for_testing()
    yield
    mp_tasks._reset_for_testing()


def _make_qdrant(facet_counts: dict[int, int] | None = None) -> MagicMock:
    q = MagicMock()
    facet_counts = facet_counts or {}

    async def fake_facet(**kwargs):
        res = MagicMock()
        hits = []
        for fid, cnt in facet_counts.items():
            h = MagicMock()
            h.value = fid
            h.count = cnt
            hits.append(h)
        res.hits = hits
        return res

    q.client.facet = AsyncMock(side_effect=fake_facet)
    q.client.scroll = AsyncMock(return_value=([], None))
    q.client.delete = AsyncMock()
    return q


def _build_test_app(path: str, qdrant_handle, *, with_auth: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await init_sqlite(path)
        await init_feed_tables(conn)
        await init_article_tables(conn)
        await init_intent_tables(conn)
        await init_match_seen_tables(conn)
        yield
        # Drain still-running BG planning/applying tasks before
        # close_sqlite so they don't observe a torn-down connection.
        from sembr.api.maintenance import _BG_TASKS

        if _BG_TASKS:
            await asyncio.gather(*list(_BG_TASKS), return_exceptions=True)
        await close_sqlite()

    app = FastAPI(lifespan=lifespan)
    app.include_router(maintenance_router)
    app.state.qdrant = qdrant_handle
    if with_auth:
        # Mount the production middleware so the /api/dashboard/maintenance/*
        # paths are gated. add_middleware uses LIFO ordering, so this is the
        # outermost wrapper.
        from sembr.dashboard.auth import DashboardTokenMiddleware

        app.add_middleware(DashboardTokenMiddleware)
    return app


@pytest.fixture
def app_factory():
    """Build a FastAPI test app whose lifespan opens a temp SQLite DB and
    attaches a Qdrant stub on ``app.state.qdrant``.
    """
    paths: list[str] = []

    def _build(qdrant_handle):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        paths.append(path)
        return _build_test_app(path, qdrant_handle)

    yield _build

    for path in paths:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path + suffix)


@pytest.fixture
def app_with_auth_factory(monkeypatch):
    """Variant that mounts ``DashboardTokenMiddleware`` so the maintenance
    endpoints actually require ``X-Dashboard-Token``. Resets pydantic-settings
    LRU cache so the env override is picked up.
    """
    paths: list[str] = []

    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-test-token")
    from sembr.config import get_settings

    get_settings.cache_clear()

    def _build(qdrant_handle):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        paths.append(path)
        return _build_test_app(path, qdrant_handle, with_auth=True)

    yield _build

    get_settings.cache_clear()
    for path in paths:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path + suffix)


def _wait_for_status(client: TestClient, task_id: str, target: str, timeout: float = 5.0) -> dict:
    """Poll the status endpoint (sync, via TestClient) until task.status == target."""
    deadline = time.monotonic() + timeout
    last_status = None
    data: dict = {}
    while time.monotonic() < deadline:
        resp = client.get(f"/api/dashboard/maintenance/manual_prune/{task_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        last_status = data["status"]
        if last_status == target:
            return data
        if last_status == "error":
            return data
        time.sleep(0.05)
    raise AssertionError(
        f"task {task_id} did not reach {target!r} within {timeout}s; "
        f"last status={last_status!r} error={data.get('error')!r}"
    )


# ---------------------------------------------------------------------------
# feed_universe
# ---------------------------------------------------------------------------


def test_feed_universe_alive_and_deleted(app_factory):
    qdrant = _make_qdrant({6: 12, 12: 88, 99: 34})
    app = app_factory(qdrant)
    with TestClient(app) as c:
        # Seed feed 6, 12 in SQLite; 99 only in Qdrant → "deleted"

        # Insert via raw conn since there's no public seed helper here
        async def _seed():
            from sembr.db.sqlite import get_conn

            conn = get_conn()
            await conn.execute(
                "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
                "VALUES (6, 'CNN', 'http://cnn', 30)"
            )
            await conn.execute(
                "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
                "VALUES (12, 'Econ', 'http://econ', 30)"
            )
            await conn.commit()

        asyncio.run(_seed())

        resp = c.get("/api/dashboard/maintenance/feed_universe")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        alive_ids = {f["id"] for f in data["alive"]}
        deleted_ids = {f["id"] for f in data["deleted"]}
        assert alive_ids == {6, 12}
        assert deleted_ids == {99}
        # name carried for alive, None for deleted
        assert {f["id"]: f["name"] for f in data["alive"]} == {6: "CNN", 12: "Econ"}


# ---------------------------------------------------------------------------
# manual_prune POST + GET
# ---------------------------------------------------------------------------


def test_manual_prune_news_planning_to_done(app_factory):
    """S6: POST → planning → planned (with plan_summary) → confirm → applying → done."""
    qdrant = _make_qdrant({6: 3})
    md5s = [f"{i:032x}" for i in range(3)]
    points = []
    for m in md5s:
        p = MagicMock()
        p.id = md5_to_uuid(m)
        points.append(p)
    qdrant.client.scroll = AsyncMock(return_value=(points, None))

    app = app_factory(qdrant)
    with TestClient(app) as c:

        async def _seed():
            from sembr.db.sqlite import get_conn

            conn = get_conn()
            await conn.execute(
                "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
                "VALUES (6, 'CNN', 'http://cnn', 30)"
            )
            for m in md5s:
                await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, 6)", (m,))
            await conn.commit()

        asyncio.run(_seed())

        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            json={
                "target": "news",
                "feed_ids": [6],
                "older_than_days": 35,
            },
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        task_id = body["task_id"]
        assert body["status_url"].endswith(task_id)

        planned = _wait_for_status(c, task_id, "planned")
        assert planned["plan_summary"]["target"] == "news"
        assert planned["plan_summary"]["total_would_delete"] == 3

        confirm = c.post(f"/api/dashboard/maintenance/manual_prune/{task_id}/confirm")
        assert confirm.status_code == 202, confirm.text
        assert confirm.json()["status"] == "applying"

        done = _wait_for_status(c, task_id, "done")
        assert done["result_summary"]["deleted_qdrant"] == 3
        assert done["result_summary"]["deleted_feed_items"] == 3
        qdrant.client.delete.assert_awaited()


def test_manual_prune_confirm_wrong_state_returns_409(app_factory):
    """A planning task can't be confirmed yet — 409 Conflict."""
    qdrant = _make_qdrant({6: 1})

    # Stall planning until we explicitly allow it to complete.
    gate = asyncio.Event()

    async def slow_facet(**kwargs):
        await gate.wait()
        res = MagicMock()
        res.hits = []
        return res

    qdrant.client.facet = AsyncMock(side_effect=slow_facet)

    app = app_factory(qdrant)
    with TestClient(app) as c:

        async def _seed():
            from sembr.db.sqlite import get_conn

            conn = get_conn()
            await conn.execute(
                "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
                "VALUES (6, 'CNN', 'http://cnn', 30)"
            )
            await conn.commit()

        asyncio.run(_seed())

        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            json={"target": "news", "feed_ids": [6], "older_than_days": 35},
        )
        task_id = resp.json()["task_id"]

        # Status is still 'planning' — confirm must 409
        confirm = c.post(f"/api/dashboard/maintenance/manual_prune/{task_id}/confirm")
        assert confirm.status_code == 409, confirm.text
        assert "planned" in confirm.json()["detail"]

        # Release the gate so the BG task can finish before TestClient teardown.
        async def _release():
            gate.set()

        asyncio.run(_release())


def test_manual_prune_get_404(app_factory):
    qdrant = _make_qdrant({})
    app = app_factory(qdrant)
    with TestClient(app) as c:
        resp = c.get("/api/dashboard/maintenance/manual_prune/does-not-exist")
        assert resp.status_code == 404


def test_manual_prune_validation_rejects_empty_feed_ids(app_factory):
    qdrant = _make_qdrant({})
    app = app_factory(qdrant)
    with TestClient(app) as c:
        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            json={"target": "news", "feed_ids": [], "older_than_days": 35},
        )
        assert resp.status_code == 422


def test_manual_prune_dead_path_does_not_call_qdrant(app_factory):
    """S8 dead branch: target=dead must NOT touch Qdrant on planning or apply."""
    qdrant = _make_qdrant({})
    app = app_factory(qdrant)
    with TestClient(app) as c:

        async def _seed():
            from datetime import datetime, timedelta

            from sembr.db.sqlite import get_conn

            conn = get_conn()
            await conn.execute(
                "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
                "VALUES (7, 'X', 'http://x', 30)"
            )
            old = (datetime.now(UTC) - timedelta(days=20)).isoformat()
            for i in range(3):
                await conn.execute(
                    "INSERT INTO dead_articles "
                    "(md5, feed_id, url, title, body, published_at, error_message, failed_at) "
                    "VALUES (?, 7, 'u', 't', 'b', NULL, 'err', ?)",
                    (f"{i:032x}", old),
                )
            await conn.commit()

        asyncio.run(_seed())

        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            json={"target": "dead", "feed_ids": [7], "older_than_days": 14},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        planned = _wait_for_status(c, task_id, "planned")
        assert planned["plan_summary"]["total_would_delete"] == 3

        confirm = c.post(f"/api/dashboard/maintenance/manual_prune/{task_id}/confirm")
        assert confirm.status_code == 202
        done = _wait_for_status(c, task_id, "done")
        assert done["result_summary"]["deleted_dead_articles"] == 3

        # Qdrant facet/scroll/delete must never have been called.
        qdrant.client.facet.assert_not_called()
        qdrant.client.scroll.assert_not_called()
        qdrant.client.delete.assert_not_called()


# ---------------------------------------------------------------------------
# 🟡-4 regression: DashboardTokenMiddleware actually gates the endpoints
# ---------------------------------------------------------------------------


def test_feed_universe_unauthenticated_returns_401(app_with_auth_factory):
    """Without X-Dashboard-Token (and the env-set token is non-empty),
    /feed_universe must 401 — guards against accidentally renaming the
    router prefix out of the middleware's protected list."""
    qdrant = _make_qdrant({})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.get("/api/dashboard/maintenance/feed_universe")
        assert resp.status_code == 401, resp.text


def test_feed_universe_authenticated_succeeds(app_with_auth_factory):
    qdrant = _make_qdrant({6: 5})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.get(
            "/api/dashboard/maintenance/feed_universe",
            headers={"X-Dashboard-Token": "secret-test-token"},
        )
        assert resp.status_code == 200, resp.text


def test_manual_prune_post_unauthenticated_returns_401(app_with_auth_factory):
    qdrant = _make_qdrant({})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            json={"target": "news", "feed_ids": [1], "older_than_days": 35},
        )
        assert resp.status_code == 401, resp.text


def test_manual_prune_post_with_wrong_token_returns_401(app_with_auth_factory):
    qdrant = _make_qdrant({})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.post(
            "/api/dashboard/maintenance/manual_prune",
            headers={"X-Dashboard-Token": "wrong-token"},
            json={"target": "news", "feed_ids": [1], "older_than_days": 35},
        )
        assert resp.status_code == 401, resp.text


def test_manual_prune_status_get_unauthenticated_returns_401(app_with_auth_factory):
    """GET /manual_prune/{task_id} must 401 without a token — security regression
    guard against the router prefix being moved out of /api/dashboard/."""
    qdrant = _make_qdrant({})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.get("/api/dashboard/maintenance/manual_prune/any-task-id")
        assert resp.status_code == 401, resp.text


def test_manual_prune_confirm_unauthenticated_returns_401(app_with_auth_factory):
    """POST /manual_prune/{task_id}/confirm must 401 without a token — same
    rationale as the status GET."""
    qdrant = _make_qdrant({})
    app = app_with_auth_factory(qdrant)
    with TestClient(app) as c:
        resp = c.post("/api/dashboard/maintenance/manual_prune/any-task-id/confirm")
        assert resp.status_code == 401, resp.text

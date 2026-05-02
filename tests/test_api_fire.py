"""Tests for POST /intents/{id}/fire and GET /intents/{id}/fire/{task_id} (DD8)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sembr.matcher.fire_tasks import _reset_for_testing, get_task
from sembr.matcher.scan import ScanOptions


@pytest.fixture(autouse=True)
def reset_tasks():
    _reset_for_testing()
    yield
    _reset_for_testing()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_intent(intent_id: int = 1):
    from sembr.models import CronSchedule, Intent

    return Intent(
        id=intent_id,
        name="test-intent",
        text="quantum computing",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=CronSchedule(preset="daily", lookback_seconds=3600, skip_seen=True),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )


def _fake_match(article_id: str = "art-1", score: float = 0.82):
    m = MagicMock()
    m.article_id = article_id
    m.score = score
    m.payload = {"title": "Test Article", "url": "https://example.com", "published_at": None}
    return m


def _default_options() -> ScanOptions:
    return ScanOptions(
        lookback_seconds=3600,
        threshold=0.75,
        skip_seen=True,
        feed_ids=None,
        write_match_seen=False,
    )


def _make_app(on_match=None):
    from fastapi import FastAPI
    from sembr.api.fire import router

    app = FastAPI()
    app.include_router(router)
    app.state.qdrant = MagicMock()
    app.state.qdrant.client = MagicMock()
    app.state.on_match = on_match
    return app


# ---------------------------------------------------------------------------
# AC1: POST returns 202 + task_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_fire_returns_202_with_task_id():
    app = _make_app()
    intent = _fake_intent()

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/fire")

    assert resp.status_code == 202
    body = resp.json()
    assert "task_id" in body
    assert "status_url" in body
    assert body["status_url"] == f"/intents/1/fire/{body['task_id']}"


# ---------------------------------------------------------------------------
# AC2: GET after scan_once completes returns matches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_fire_status_returns_matches():
    from sembr.api.fire import _fire_run
    from sembr.matcher.fire_tasks import create_task

    app = _make_app()
    intent = _fake_intent(1)
    matches = [_fake_match("art-1", 0.85), _fake_match("art-2", 0.78)]
    task = create_task(1)
    options = _default_options()

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        await _fire_run(task, options, app)

    assert task.status == "done"
    assert task.match_count == 2
    assert len(task.matches) == 2
    assert task.matches[0]["article_id"] == "art-1"
    assert task.matches[0]["score"] == 0.85
    assert task.pushed is False  # on_match is None


# ---------------------------------------------------------------------------
# AC3: on_match is called when matches found, pushed=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_run_calls_on_match_when_matches():
    from sembr.api.fire import _fire_run
    from sembr.matcher.fire_tasks import create_task

    on_match = AsyncMock()
    app = _make_app(on_match=on_match)
    intent = _fake_intent(1)
    matches = [_fake_match()]
    task = create_task(1)

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=matches)),
    ):
        await _fire_run(task, _default_options(), app)

    on_match.assert_awaited_once_with(matches)
    assert task.pushed is True
    assert task.status == "done"


# ---------------------------------------------------------------------------
# AC4: Zero hits — on_match NOT called, match_count=0
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_run_zero_hits_no_on_match():
    from sembr.api.fire import _fire_run
    from sembr.matcher.fire_tasks import create_task

    on_match = AsyncMock()
    app = _make_app(on_match=on_match)
    intent = _fake_intent(1)
    task = create_task(1)

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        await _fire_run(task, _default_options(), app)

    on_match.assert_not_awaited()
    assert task.pushed is False
    assert task.match_count == 0
    assert task.status == "done"


# ---------------------------------------------------------------------------
# AC5: Unknown intent → status="error"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_run_unknown_intent_sets_error():
    from sembr.api.fire import _fire_run
    from sembr.matcher.fire_tasks import create_task

    app = _make_app()
    task = create_task(99)

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=None)),
    ):
        await _fire_run(task, _default_options(), app)

    assert task.status == "error"
    assert task.finished_at is not None


# ---------------------------------------------------------------------------
# AC6: GET 404 for unknown task_id
# ---------------------------------------------------------------------------


def test_get_fire_status_404_for_unknown_task():
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/intents/1/fire/nonexistent-task-id")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC7: GET 404 when task belongs to different intent_id
# ---------------------------------------------------------------------------


def test_get_fire_status_404_wrong_intent():
    from sembr.matcher.fire_tasks import create_task

    app = _make_app()
    task = create_task(intent_id=2)

    client = TestClient(app)
    resp = client.get(f"/intents/1/fire/{task.task_id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC8: POST 404 when intent not found
# ---------------------------------------------------------------------------


def test_post_fire_404_when_intent_not_found():
    app = _make_app()

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=None)),
    ):
        client = TestClient(app)
        resp = client.post("/intents/999/fire")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AC9: Query param overrides applied to ScanOptions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_fire_query_params_override_intent_defaults():
    app = _make_app()
    intent = _fake_intent(1)
    captured_options = {}

    async def _fake_scan_once(intent, options, conn, qdrant_client):
        captured_options.update(
            lookback=options.lookback_seconds,
            threshold=options.threshold,
            skip_seen=options.skip_seen,
        )
        return []

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=_fake_scan_once),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/fire?lookback=600&threshold=0.80&skip_seen=false")

    assert resp.status_code == 202
    await asyncio.sleep(0.05)
    assert captured_options.get("lookback") == 600
    assert abs(captured_options.get("threshold", 0) - 0.80) < 1e-9
    assert captured_options.get("skip_seen") is False


# ---------------------------------------------------------------------------
# F7: Rate limit — second fire within 60s returns 429
# ---------------------------------------------------------------------------


def test_post_fire_rate_limit_429_on_second_request():
    app = _make_app()
    intent = _fake_intent(1)

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=intent)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        first = client.post("/intents/1/fire")
        second = client.post("/intents/1/fire")

    assert first.status_code == 202
    assert second.status_code == 429


def test_post_fire_rate_limit_independent_per_intent():
    """Rate limit on intent 1 does not affect intent 2."""
    app = _make_app()
    intent1 = _fake_intent(1)
    intent2 = _fake_intent(2)

    def _get_intent_side_effect(conn, iid):
        return intent1 if iid == 1 else intent2

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(side_effect=_get_intent_side_effect)),
        patch("sembr.api.fire.scan_once", new=AsyncMock(return_value=[])),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        r1 = client.post("/intents/1/fire")
        r2 = client.post("/intents/2/fire")

    assert r1.status_code == 202
    assert r2.status_code == 202


# ---------------------------------------------------------------------------
# EventSchedule intent → 409
# ---------------------------------------------------------------------------


def test_post_fire_event_intent_returns_409():
    """POST /fire on an EventSchedule intent must return 409 (event path, not fire path)."""
    from sembr.models import EventSchedule, Intent

    event_intent = Intent(
        id=10,
        name="event-intent",
        text="tracking topic",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=EventSchedule(trigger_count=3, max_wait_seconds=1800),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    app = _make_app()

    with (
        patch("sembr.api.fire.get_conn", return_value=MagicMock()),
        patch("sembr.api.fire.get_intent", new=AsyncMock(return_value=event_intent)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/10/fire")

    assert resp.status_code == 409
    assert "event-mode" in resp.json()["detail"]

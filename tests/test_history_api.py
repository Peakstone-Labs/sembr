# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr/api/history.py — GET / DELETE / POST / GET status."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from sembr.matcher.backfill_tasks import (
    _reset_for_testing,
    create_task,
    get_intent_lock,
    release_intent,
    try_acquire_intent,
)


@pytest.fixture(autouse=True)
def reset_tasks():
    _reset_for_testing()
    yield
    _reset_for_testing()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cron_intent(intent_id: int = 1):
    from sembr.models import CronSchedule, Intent  # noqa: PLC0415

    return Intent(
        id=intent_id,
        name="hist-intent",
        text="quantum computing",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=CronSchedule(preset="daily", hour=9, minute=0, lookback_seconds=86400),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        extraction_enabled=False,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _event_intent(intent_id: int = 1):
    from sembr.models import EventSchedule, Intent  # noqa: PLC0415

    return Intent(
        id=intent_id,
        name="event-intent",
        text="x",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=EventSchedule(trigger_count=3),
        feed_filter=None,
        timezone="UTC",
        language="en",
        system_template="default",
        instruction_template="default",
        extraction_enabled=False,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def _make_app():
    from fastapi import FastAPI  # noqa: PLC0415

    from sembr.api.history import router  # noqa: PLC0415

    app = FastAPI()
    app.include_router(router)
    app.state.qdrant = MagicMock()
    app.state.qdrant.client = MagicMock()
    app.state.scheduler = MagicMock()
    app.state.summary_pipeline = MagicMock()
    return app


# ---------------------------------------------------------------------------
# GET /intents/{id}/history
# ---------------------------------------------------------------------------


def test_get_history_happy() -> None:
    app = _make_app()
    rows = [
        {
            "id": 10,
            "intent_id": 1,
            "run_at": "2026-05-26T09:00:00Z",
            "summary": "hi",
            "citations": [],
        }
    ]
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.list_summaries", new=AsyncMock(return_value=rows)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/intents/1/history?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["intent_id"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert body["rows"] == rows


def test_get_history_intent_not_found() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=None)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/intents/999/history")
    assert resp.status_code == 404


def test_get_history_limit_validation() -> None:
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/intents/1/history?limit=0")
    assert resp.status_code == 422
    resp = client.get("/intents/1/history?limit=10000")
    assert resp.status_code == 422


def test_get_history_default_pagination() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.list_summaries", new=AsyncMock(return_value=[])) as ls_mock,
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/intents/1/history")
    assert resp.status_code == 200
    # default limit=100, offset=0
    ls_mock.assert_awaited_once()
    assert ls_mock.await_args.kwargs == {"limit": 100, "offset": 0}


# ---------------------------------------------------------------------------
# DELETE /intents/{id}/history/{row_id}
# ---------------------------------------------------------------------------


def test_delete_history_happy() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.delete_summary", new=AsyncMock(return_value=True)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/intents/1/history/42")
    assert resp.status_code == 204


def test_delete_history_intent_not_found() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=None)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/intents/1/history/42")
    assert resp.status_code == 404


def test_delete_history_row_not_found() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.delete_summary", new=AsyncMock(return_value=False)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.delete("/intents/1/history/9999")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /intents/{id}/backfill
# ---------------------------------------------------------------------------


def test_post_backfill_returns_202_with_task_id() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch(
            "sembr.api.history.probe_oldest_news_ts", new=AsyncMock(return_value=0)
        ),  # Qdrant covers everything
        patch("sembr.api.history.run_backfill", new=AsyncMock()),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/backfill", json={"past_runs": 3})
    assert resp.status_code == 202
    body = resp.json()
    assert "task_id" in body
    assert body["status_url"].endswith(f"/intents/1/backfill/{body['task_id']}")


def test_post_backfill_intent_not_found() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=None)),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/999/backfill", json={"past_runs": 3})
    assert resp.status_code == 404


def test_post_backfill_event_intent_rejected() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_event_intent())),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/backfill", json={"past_runs": 3})
    assert resp.status_code == 409
    assert "cron" in resp.json()["detail"]


def test_post_backfill_past_runs_validation() -> None:
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    # ge=1
    resp = client.post("/intents/1/backfill", json={"past_runs": 0})
    assert resp.status_code == 422
    # le=365
    resp = client.post("/intents/1/backfill", json={"past_runs": 366})
    assert resp.status_code == 422


def test_post_backfill_qdrant_depth_insufficient() -> None:
    app = _make_app()
    # Oldest news_ts in the future → all fire-times older than coverage.
    far_future_ts = int(datetime.now(UTC).timestamp()) + 10_000_000
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch(
            "sembr.api.history.probe_oldest_news_ts",
            new=AsyncMock(return_value=far_future_ts),
        ),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/backfill", json={"past_runs": 3})
    assert resp.status_code == 422
    payload = resp.json()["detail"]
    assert payload["code"] == "qdrant_depth_insufficient"
    assert "oldest_date" in payload
    assert "max_backfillable_runs" in payload
    assert payload["max_backfillable_runs"] == 0


def test_post_backfill_lock_409_when_in_progress() -> None:
    app = _make_app()
    # Pre-acquire lock to simulate concurrent in-flight backfill — use the
    # public try_acquire_intent so the test exercises the same contract the
    # production POST handler relies on.
    assert try_acquire_intent(1) is True

    try:
        with (
            patch("sembr.api.history.get_conn", return_value=MagicMock()),
            patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
            patch(
                "sembr.api.history.probe_oldest_news_ts",
                new=AsyncMock(return_value=0),
            ),
        ):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/intents/1/backfill", json={"past_runs": 3})
    finally:
        release_intent(1)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "backfill_in_progress"


def test_post_backfill_creates_task_and_lock_taken() -> None:
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.probe_oldest_news_ts", new=AsyncMock(return_value=0)),
        patch("sembr.api.history.run_backfill", new=AsyncMock()),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/backfill", json={"past_runs": 5})
    assert resp.status_code == 202
    # Lock for intent 1 must have been acquired (still locked because the
    # patched run_backfill is a no-op AsyncMock that doesn't release).
    assert get_intent_lock(1).locked()


def test_post_backfill_releases_lock_on_spawn_failure() -> None:
    """Regression guard: review loop 1 🟡-5.

    If anything between lock-acquire and asyncio.create_task raises, the lock
    must be released — otherwise the intent is permanently 409 until restart.
    """
    app = _make_app()

    def boom(*a, **kw):
        raise RuntimeError("simulated create_task failure")

    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_cron_intent())),
        patch("sembr.api.history.probe_oldest_news_ts", new=AsyncMock(return_value=0)),
        patch("sembr.api.history.run_backfill", new=MagicMock()),
        patch("sembr.api.history.asyncio.create_task", new=boom),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/intents/1/backfill", json={"past_runs": 3})
    # 500 (handler raised) — but importantly the lock must be released so the
    # next request can succeed.
    assert resp.status_code == 500
    assert not get_intent_lock(1).locked(), "lock leaked after spawn failure"


# ---------------------------------------------------------------------------
# GET /intents/{id}/backfill/{task_id}
# ---------------------------------------------------------------------------


def test_get_backfill_status_happy() -> None:
    app = _make_app()
    task = create_task(intent_id=1, total=5)
    task.progress.done = 2
    task.progress.skipped = 1

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/intents/1/backfill/{task.task_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task_id"] == task.task_id
    assert body["intent_id"] == 1
    assert body["status"] == "running"
    assert body["progress"]["done"] == 2
    assert body["progress"]["skipped"] == 1
    assert body["progress"]["total"] == 5
    assert body["error_reason"] is None


def test_get_backfill_status_404_wrong_intent() -> None:
    app = _make_app()
    task = create_task(intent_id=1, total=5)
    client = TestClient(app, raise_server_exceptions=False)
    # Status URL targets intent 2 but the task is for intent 1 → must 404
    resp = client.get(f"/intents/2/backfill/{task.task_id}")
    assert resp.status_code == 404


def test_get_backfill_status_404_unknown_task() -> None:
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/intents/1/backfill/nonexistent-task-id")
    assert resp.status_code == 404


def test_get_backfill_status_terminal_progress_shape() -> None:
    """Done task: status='done', finished_at present, progress fields intact."""
    app = _make_app()
    task = create_task(intent_id=1, total=3)
    task.status = "done"
    task.finished_at = datetime.now(UTC)
    task.progress.done = 3

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get(f"/intents/1/backfill/{task.task_id}")
    body = resp.json()
    assert body["status"] == "done"
    assert body["finished_at"] is not None
    assert body["progress"]["done"] == 3

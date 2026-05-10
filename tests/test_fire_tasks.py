"""Unit tests for DD4: FireTask in-memory state store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from sembr.matcher.fire_tasks import (
    FireTask,
    _fire_tasks,
    _last_fire_at,
    _reset_for_testing,
    check_and_record_fire,
    create_task,
    get_task,
    sweep_expired,
    throttle_check,
)


@pytest.fixture(autouse=True)
def reset_tasks():
    _reset_for_testing()
    yield
    _reset_for_testing()


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------


def test_create_task_returns_running_task() -> None:
    task = create_task(intent_id=5)
    assert task.intent_id == 5
    assert task.status == "running"
    assert task.task_id in _fire_tasks
    assert task.finished_at is None
    assert task.match_count == 0
    assert task.matches == []
    assert task.pushed is False
    assert task.push_error is None


def test_create_task_unique_ids() -> None:
    t1 = create_task(1)
    t2 = create_task(1)
    assert t1.task_id != t2.task_id


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------


def test_get_task_returns_task() -> None:
    task = create_task(3)
    fetched = get_task(task.task_id)
    assert fetched is task


def test_get_task_unknown_returns_none() -> None:
    assert get_task("nonexistent-id") is None


# ---------------------------------------------------------------------------
# Mutation after background work
# ---------------------------------------------------------------------------


def test_task_status_updated_after_completion() -> None:
    task = create_task(1)
    task.status = "done"
    task.match_count = 7
    task.pushed = True
    task.finished_at = datetime.now(timezone.utc)

    fetched = get_task(task.task_id)
    assert fetched is not None
    assert fetched.status == "done"
    assert fetched.match_count == 7
    assert fetched.pushed is True
    assert fetched.finished_at is not None


# ---------------------------------------------------------------------------
# TTL sweep
# ---------------------------------------------------------------------------


def test_sweep_expired_removes_old_tasks() -> None:
    task = create_task(1)
    # Backdate the task's _created_at to beyond TTL
    task._created_at = datetime.now(timezone.utc) - timedelta(seconds=3700)

    removed = sweep_expired(ttl_seconds=3600)
    assert removed == 1
    assert get_task(task.task_id) is None


def test_sweep_expired_keeps_fresh_tasks() -> None:
    task = create_task(2)
    removed = sweep_expired(ttl_seconds=3600)
    assert removed == 0
    assert get_task(task.task_id) is not None


def test_sweep_expired_mixed() -> None:
    old = create_task(1)
    old._created_at = datetime.now(timezone.utc) - timedelta(seconds=3700)
    fresh = create_task(2)

    removed = sweep_expired(ttl_seconds=3600)
    assert removed == 1
    assert get_task(old.task_id) is None
    assert get_task(fresh.task_id) is not None


def test_sweep_expired_empty_no_error() -> None:
    assert sweep_expired() == 0


# ---------------------------------------------------------------------------
# check_and_record_fire (D-A9 / external-fire-api)
# ---------------------------------------------------------------------------


def test_check_and_record_fire_first_call_returns_true_and_records() -> None:
    """First call for an intent: True + writes _last_fire_at."""
    assert check_and_record_fire(42) is True
    assert 42 in _last_fire_at


def test_check_and_record_fire_within_window_returns_false() -> None:
    """Second call inside the rate window: False; existing record unchanged."""
    assert check_and_record_fire(7) is True
    recorded = _last_fire_at[7]
    assert check_and_record_fire(7) is False
    # Rejected request must not bump the timestamp; otherwise consumers can
    # extend their own window indefinitely by hammering the endpoint.
    assert _last_fire_at[7] == recorded


def test_check_and_record_fire_after_window_returns_true() -> None:
    """Window expired: True; timestamp advances."""
    assert check_and_record_fire(8) is True
    # Backdate to outside the rate window
    _last_fire_at[8] = datetime.now(timezone.utc) - timedelta(seconds=120)
    old = _last_fire_at[8]
    assert check_and_record_fire(8) is True
    assert _last_fire_at[8] > old


def test_check_and_record_fire_independent_per_intent() -> None:
    assert check_and_record_fire(1) is True
    assert check_and_record_fire(2) is True
    assert check_and_record_fire(1) is False
    assert check_and_record_fire(2) is False


def test_check_and_record_fire_shares_bucket_with_async_path() -> None:
    """create_task (async fire) and check_and_record_fire (sync fire) share
    _last_fire_at, so triggering one must immediately rate-limit the other.
    Validates the design D-A5 single-bucket promise."""
    create_task(intent_id=99)
    # Async path created the task and stamped _last_fire_at[99]; sync path
    # must therefore reject within the window.
    assert check_and_record_fire(99) is False


def test_check_and_record_fire_blocks_subsequent_async_path() -> None:
    """Symmetric: sync path stamping must block async ``throttle_check``."""
    assert check_and_record_fire(5) is True
    assert throttle_check(5) is False


def test_check_and_record_fire_sync_signature() -> None:
    """D-A9 hard constraint: must remain a sync function. Adding async/await
    re-opens the TOCTOU window the helper was created to close."""
    import asyncio  # noqa: PLC0415
    assert not asyncio.iscoroutinefunction(check_and_record_fire)

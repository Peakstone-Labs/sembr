"""Unit tests for DD4: FireTask in-memory state store."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from sembr.matcher.fire_tasks import (
    FireTask,
    _fire_tasks,
    _reset_for_testing,
    create_task,
    get_task,
    sweep_expired,
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

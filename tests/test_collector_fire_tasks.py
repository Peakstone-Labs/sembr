# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr.collector.fire_tasks — task store + sweep (D13)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sembr.collector.fire_tasks import (
    FeedFireTask,
    _reset_for_testing,
    create_task,
    get_task,
    sweep_expired,
    throttle_check,
)


@pytest.fixture(autouse=True)
def reset():
    _reset_for_testing()
    yield
    _reset_for_testing()


def test_create_task_returns_running_task() -> None:
    task = create_task(feed_id=1, dry_run=False)
    assert task.status == "running"
    assert task.feed_id == 1
    assert task.dry_run is False
    assert task.task_id != ""


def test_get_task_returns_task() -> None:
    task = create_task(feed_id=1, dry_run=False)
    fetched = get_task(task.task_id)
    assert fetched is task


def test_get_task_unknown_id_returns_none() -> None:
    assert get_task("nonexistent-id") is None


def test_dry_run_does_not_consume_rate_limit() -> None:
    # Create a dry_run task — should not update _last_fire_at
    create_task(feed_id=1, dry_run=True)
    # Real fire should still be allowed
    assert throttle_check(feed_id=1) is True


def test_real_run_consumes_rate_limit() -> None:
    create_task(feed_id=1, dry_run=False)
    # Second real fire within 60s should be blocked
    assert throttle_check(feed_id=1) is False


def test_throttle_check_passes_when_no_prior_fire() -> None:
    assert throttle_check(feed_id=99) is True


def test_throttle_check_passes_after_rate_limit_expires() -> None:
    create_task(feed_id=2, dry_run=False)
    # Fake time by using a very short rate window
    assert throttle_check(feed_id=2, rate_seconds=0) is True


def test_sweep_expired_removes_old_tasks() -> None:
    task = create_task(feed_id=1, dry_run=False)
    # Backdate _created_at to simulate expiry
    from sembr.collector import fire_tasks as _ft

    _ft._feed_fire_tasks[task.task_id]._created_at = datetime.now(timezone.utc) - timedelta(
        seconds=7200
    )

    removed = sweep_expired(ttl_seconds=3600)
    assert removed == 1
    assert get_task(task.task_id) is None


def test_sweep_expired_keeps_recent_tasks() -> None:
    task = create_task(feed_id=1, dry_run=False)
    removed = sweep_expired(ttl_seconds=3600)
    assert removed == 0
    assert get_task(task.task_id) is not None

# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr/matcher/cron_recall.py — past_n_fire_times."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from itertools import pairwise

import pytest

from sembr.matcher.cron_recall import past_n_fire_times
from sembr.models import CronSchedule


def test_past_n_fire_times_hourly() -> None:
    schedule = CronSchedule(preset="hourly", minute=15)
    now = datetime(2026, 5, 27, 12, 30, tzinfo=UTC)
    out = past_n_fire_times(schedule, "UTC", 5, now=now)
    assert [(t.hour, t.minute) for t in out] == [(11, 15), (10, 15), (9, 15), (8, 15), (7, 15)]
    for t in out:
        assert t.tzinfo == UTC
        assert t.date() == datetime(2026, 5, 27).date()


def test_past_n_fire_times_daily() -> None:
    schedule = CronSchedule(preset="daily", hour=9, minute=0)
    # Shanghai is UTC+8 fixed (no DST); 2026-05-27T12:00Z = 2026-05-27 20:00 CST
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    out = past_n_fire_times(schedule, "Asia/Shanghai", 3, now=now)
    # Today's 09:00 CST = 01:00 UTC. We skip the current day per design semantics.
    # Newest past = yesterday 09:00 CST = 2026-05-26 01:00 UTC.
    expected = [
        datetime(2026, 5, 26, 1, 0, tzinfo=UTC),
        datetime(2026, 5, 25, 1, 0, tzinfo=UTC),
        datetime(2026, 5, 24, 1, 0, tzinfo=UTC),
    ]
    assert out == expected


def test_past_n_fire_times_weekly() -> None:
    # 2026-05-27 is a Wednesday. weekly preset Mon 09:00 → last Monday 09:00,
    # the Monday before that, and one more.
    schedule = CronSchedule(preset="weekly", weekday="mon", hour=9, minute=0)
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)  # Wed
    out = past_n_fire_times(schedule, "UTC", 3, now=now)
    # This week's Monday = 2026-05-25; per skip-current-period, newest past
    # is the Monday from the *previous* week = 2026-05-18.
    expected = [
        datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
        datetime(2026, 5, 11, 9, 0, tzinfo=UTC),
        datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
    ]
    assert out == expected


def test_past_n_fire_times_skip_future() -> None:
    """Daily preset hour=15, now=14:00 — must not emit today's 15:00 (future)."""
    schedule = CronSchedule(preset="daily", hour=15, minute=0)
    now = datetime(2026, 5, 27, 14, 0, tzinfo=UTC)
    out = past_n_fire_times(schedule, "UTC", 2, now=now)
    expected = [
        datetime(2026, 5, 26, 15, 0, tzinfo=UTC),  # yesterday 15:00
        datetime(2026, 5, 25, 15, 0, tzinfo=UTC),  # day-before 15:00
    ]
    assert out == expected
    assert all(t < now for t in out)


def test_past_n_fire_times_skip_current_hourly() -> None:
    """Hourly minute=15, now=12:30 — must skip 12:15 even though it's already in the past."""
    schedule = CronSchedule(preset="hourly", minute=15)
    now = datetime(2026, 5, 27, 12, 30, tzinfo=UTC)
    out = past_n_fire_times(schedule, "UTC", 1, now=now)
    assert out == [datetime(2026, 5, 27, 11, 15, tzinfo=UTC)]


def test_past_n_fire_times_skip_current_daily() -> None:
    """Daily preset hour=9, now=15:00 (past today's 09:00) — still skip today's 09:00."""
    schedule = CronSchedule(preset="daily", hour=9, minute=0)
    now = datetime(2026, 5, 27, 15, 0, tzinfo=UTC)
    out = past_n_fire_times(schedule, "UTC", 1, now=now)
    assert out == [datetime(2026, 5, 26, 9, 0, tzinfo=UTC)]


def test_past_n_fire_times_default_now_is_utcnow() -> None:
    schedule = CronSchedule(preset="hourly", minute=0)
    out = past_n_fire_times(schedule, "UTC", 3)
    assert len(out) == 3
    # All entries must be in the past relative to wall clock.
    now = datetime.now(UTC)
    for t in out:
        assert t < now
    # Strictly decreasing (newest first).
    for a, b in pairwise(out):
        assert a > b


def test_past_n_fire_times_n_zero() -> None:
    schedule = CronSchedule(preset="daily", hour=9)
    assert past_n_fire_times(schedule, "UTC", 0) == []


def test_past_n_fire_times_n_negative() -> None:
    schedule = CronSchedule(preset="daily", hour=9)
    assert past_n_fire_times(schedule, "UTC", -3) == []


def test_past_n_fire_times_naive_now_raises() -> None:
    schedule = CronSchedule(preset="daily", hour=9)
    with pytest.raises(ValueError, match="timezone-aware"):
        past_n_fire_times(schedule, "UTC", 1, now=datetime(2026, 5, 27, 12, 0))


def test_past_n_fire_times_invalid_timezone() -> None:
    schedule = CronSchedule(preset="daily", hour=9)
    from zoneinfo import ZoneInfoNotFoundError  # noqa: PLC0415

    with pytest.raises(ZoneInfoNotFoundError):
        past_n_fire_times(schedule, "Mars/Olympus", 1)


def test_past_n_fire_times_weekly_same_weekday() -> None:
    """When today IS the schedule's weekday, still skip this week and return last week's same day."""
    schedule = CronSchedule(preset="weekly", weekday="wed", hour=10, minute=0)
    # 2026-05-27 is Wednesday
    now = datetime(2026, 5, 27, 15, 0, tzinfo=UTC)
    out = past_n_fire_times(schedule, "UTC", 2, now=now)
    expected = [
        datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
        datetime(2026, 5, 13, 10, 0, tzinfo=UTC),
    ]
    assert out == expected


def test_past_n_fire_times_dst_docstring_present() -> None:
    """Guard against future refactor stripping the DST limitation note."""
    doc = inspect.getdoc(past_n_fire_times)
    assert doc is not None
    assert "DST" in doc
    assert "best-effort" in doc.lower()


def test_past_n_fire_times_tz_conversion_to_utc() -> None:
    """Result list must be UTC-aware regardless of input tz."""
    schedule = CronSchedule(preset="daily", hour=9, minute=0)
    now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    out = past_n_fire_times(schedule, "Asia/Tokyo", 2, now=now)
    for t in out:
        assert t.tzinfo == UTC
    # Tokyo 09:00 = 00:00 UTC. now in Tokyo = 21:00 (UTC+9), so today's 09:00 Tokyo
    # already passed — but skip-current-period still excludes it.
    expected = [
        datetime(2026, 5, 26, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 25, 0, 0, tzinfo=UTC),
    ]
    assert out == expected

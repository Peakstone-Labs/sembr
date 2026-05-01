"""Unit tests for DD10: CronTrigger scheduling in jobs.py.

Verifies that register_intent_job correctly translates CronSchedule
{preset, hour, minute, weekday} + timezone into APScheduler CronTrigger,
and that the next_fire_time is computed correctly.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from sembr.matcher.jobs import _build_cron_trigger, register_intent_job
from sembr.models import (
    CronSchedule,
    FeedFilter,
    Intent,
    IntervalSchedule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

VALID_CHANNELS = [{"type": "email", "to": ["a@example.com"]}]


def _make_intent(**kwargs) -> Intent:
    defaults = dict(
        id=1,
        name="test",
        text="test intent",
        threshold=0.75,
        enabled=True,
        channels=VALID_CHANNELS,
        tags=[],
        schedule=IntervalSchedule(seconds=3600),
        lookback_window_seconds=86400,
        first_scan_at=None,
        custom_prompt=None,
        skip_seen=True,
        feed_filter=None,
        timezone="UTC",
        language="zh",
        created_at="2026-05-01T00:00:00Z",
        updated_at="2026-05-01T00:00:00Z",
    )
    defaults.update(kwargs)
    return Intent(**defaults)


# ---------------------------------------------------------------------------
# _build_cron_trigger unit tests
# ---------------------------------------------------------------------------


def test_build_cron_trigger_daily() -> None:
    schedule = CronSchedule(preset="daily", hour=8, minute=30)
    trigger = _build_cron_trigger(schedule, "UTC")
    assert isinstance(trigger, CronTrigger)
    # Verify next fire time is tomorrow 08:30 UTC from a midnight reference
    now = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire is not None
    assert next_fire.hour == 8
    assert next_fire.minute == 30


def test_build_cron_trigger_hourly() -> None:
    schedule = CronSchedule(preset="hourly", minute=15)
    trigger = _build_cron_trigger(schedule, "UTC")
    assert isinstance(trigger, CronTrigger)
    now = datetime(2026, 5, 1, 10, 0, 0, tzinfo=timezone.utc)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire is not None
    assert next_fire.minute == 15


def test_build_cron_trigger_weekly_sat_shanghai() -> None:
    """weekly+sat+9:00+Asia/Shanghai next fire from Wednesday UTC."""
    schedule = CronSchedule(preset="weekly", weekday="sat", hour=9, minute=0)
    trigger = _build_cron_trigger(schedule, "Asia/Shanghai")
    assert isinstance(trigger, CronTrigger)

    # 2026-05-06 is a Wednesday; next Sat = 2026-05-09
    # 09:00 Asia/Shanghai (UTC+8) = 2026-05-09 01:00:00 UTC
    now = datetime(2026, 5, 6, 4, 0, 0, tzinfo=timezone.utc)  # Wed 12:00 Shanghai
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire is not None

    # APScheduler returns the datetime in the trigger's timezone; convert to UTC to compare
    next_fire_utc = next_fire.astimezone(timezone.utc)
    # 09:00 Asia/Shanghai (UTC+8) = 2026-05-09 01:00:00 UTC
    assert next_fire_utc.year == 2026
    assert next_fire_utc.month == 5
    assert next_fire_utc.day == 9
    assert next_fire_utc.hour == 1
    assert next_fire_utc.minute == 0


def test_build_cron_trigger_weekly_mon_utc() -> None:
    schedule = CronSchedule(preset="weekly", weekday="mon", hour=7, minute=0)
    trigger = _build_cron_trigger(schedule, "UTC")
    # 2026-05-06 is Wednesday; next Monday = 2026-05-11
    now = datetime(2026, 5, 6, 8, 0, 0, tzinfo=timezone.utc)
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire is not None
    assert next_fire.weekday() == 0  # Monday = 0 in Python


# ---------------------------------------------------------------------------
# register_intent_job dispatch tests
# ---------------------------------------------------------------------------


def test_register_interval_schedule_uses_interval_trigger() -> None:
    intent = _make_intent(schedule=IntervalSchedule(seconds=300))
    scheduler = MagicMock()

    register_intent_job(scheduler, intent, app=MagicMock())

    scheduler.add_job.assert_called_once()
    trigger = scheduler.add_job.call_args.kwargs["trigger"]
    assert isinstance(trigger, IntervalTrigger)


def test_register_cron_schedule_uses_cron_trigger() -> None:
    intent = _make_intent(
        schedule=CronSchedule(preset="daily", hour=6),
        timezone="Asia/Tokyo",
    )
    scheduler = MagicMock()

    register_intent_job(scheduler, intent, app=MagicMock())

    scheduler.add_job.assert_called_once()
    trigger = scheduler.add_job.call_args.kwargs["trigger"]
    assert isinstance(trigger, CronTrigger)


def test_register_cron_schedule_timezone_injected() -> None:
    """CronTrigger carries the intent's timezone."""
    intent = _make_intent(
        schedule=CronSchedule(preset="weekly", weekday="fri", hour=18),
        timezone="America/New_York",
    )
    scheduler = MagicMock()

    register_intent_job(scheduler, intent, app=MagicMock())

    trigger = scheduler.add_job.call_args.kwargs["trigger"]
    assert isinstance(trigger, CronTrigger)
    # The trigger's timezone must reflect "America/New_York"
    assert "America/New_York" in str(trigger.timezone)


def test_register_cron_job_no_next_run_time_override() -> None:
    """CronTrigger jobs don't get next_run_time overridden (fire_immediately has no effect)."""
    intent = _make_intent(
        schedule=CronSchedule(preset="hourly"),
        timezone="UTC",
    )
    scheduler = MagicMock()

    register_intent_job(scheduler, intent, app=MagicMock(), fire_immediately=True)

    call_kwargs = scheduler.add_job.call_args.kwargs
    # CronTrigger computes its own first fire time; next_run_time should be None
    assert call_kwargs.get("next_run_time") is None

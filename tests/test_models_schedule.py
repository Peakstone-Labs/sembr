"""Unit tests for Schedule discriminated union (CronSchedule / EventSchedule)."""

from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sembr.models import CronSchedule, EventSchedule, IntentCreate, Schedule

_SCHEDULE_ADAPTER: TypeAdapter[Schedule] = TypeAdapter(Schedule)

VALID_CHANNELS = [{"type": "email", "to": ["a@example.com"]}]


# ---------------------------------------------------------------------------
# CronSchedule
# ---------------------------------------------------------------------------


def test_cron_daily_no_weekday_ok() -> None:
    s = CronSchedule(preset="daily", hour=8, minute=30)
    assert s.preset == "daily"
    assert s.weekday is None


def test_cron_hourly_no_weekday_ok() -> None:
    s = CronSchedule(preset="hourly")
    assert s.weekday is None


def test_cron_weekly_with_weekday_ok() -> None:
    s = CronSchedule(preset="weekly", weekday="sat", hour=9)
    assert s.weekday == "sat"


def test_cron_weekly_without_weekday_rejected() -> None:
    with pytest.raises(ValidationError, match="weekday is required"):
        CronSchedule(preset="weekly", hour=9)


def test_cron_daily_with_weekday_rejected() -> None:
    with pytest.raises(ValidationError, match="weekday must be None"):
        CronSchedule(preset="daily", hour=9, weekday="mon")


def test_cron_hourly_with_weekday_rejected() -> None:
    with pytest.raises(ValidationError, match="weekday must be None"):
        CronSchedule(preset="hourly", weekday="fri")


def test_cron_hourly_with_nonzero_hour_rejected() -> None:
    with pytest.raises(ValidationError, match="hour is ignored"):
        CronSchedule(preset="hourly", hour=5)


def test_cron_lookback_seconds_default() -> None:
    s = CronSchedule(preset="daily")
    assert s.lookback_seconds == 86400


def test_cron_lookback_seconds_range() -> None:
    CronSchedule(preset="daily", lookback_seconds=300)
    CronSchedule(preset="daily", lookback_seconds=2592000)
    with pytest.raises(ValidationError):
        CronSchedule(preset="daily", lookback_seconds=299)
    with pytest.raises(ValidationError):
        CronSchedule(preset="daily", lookback_seconds=2592001)


def test_cron_skip_seen_default() -> None:
    s = CronSchedule(preset="daily")
    assert s.skip_seen is True


# ---------------------------------------------------------------------------
# EventSchedule
# ---------------------------------------------------------------------------


def test_event_defaults() -> None:
    s = EventSchedule()
    assert s.mode == "event"
    assert s.trigger_count == 3
    assert s.max_wait_seconds == 1800


def test_event_trigger_count_range() -> None:
    EventSchedule(trigger_count=1)
    EventSchedule(trigger_count=10)
    with pytest.raises(ValidationError):
        EventSchedule(trigger_count=0)
    with pytest.raises(ValidationError):
        EventSchedule(trigger_count=11)


def test_event_max_wait_seconds_range() -> None:
    EventSchedule(max_wait_seconds=60)
    EventSchedule(max_wait_seconds=86400)
    with pytest.raises(ValidationError):
        EventSchedule(max_wait_seconds=59)
    with pytest.raises(ValidationError):
        EventSchedule(max_wait_seconds=86401)


# ---------------------------------------------------------------------------
# Discriminated union round-trip
# ---------------------------------------------------------------------------


def test_schedule_union_interval_mode_rejected() -> None:
    """interval mode was removed; discriminator should reject it."""
    with pytest.raises(ValidationError):
        _SCHEDULE_ADAPTER.validate_python({"mode": "interval", "seconds": 3600})


def test_schedule_union_cron_roundtrip() -> None:
    raw = {"mode": "cron", "preset": "weekly", "weekday": "mon", "hour": 7, "minute": 0}
    s = _SCHEDULE_ADAPTER.validate_python(raw)
    assert isinstance(s, CronSchedule)
    assert s.preset == "weekly"
    assert s.weekday == "mon"


def test_schedule_union_event_roundtrip() -> None:
    raw = {"mode": "event", "trigger_count": 5, "max_wait_seconds": 600}
    s = _SCHEDULE_ADAPTER.validate_python(raw)
    assert isinstance(s, EventSchedule)
    assert s.trigger_count == 5
    assert s.max_wait_seconds == 600
    dumped = s.model_dump()
    assert dumped == {"mode": "event", "trigger_count": 5, "max_wait_seconds": 600}


def test_schedule_union_unknown_mode_rejected() -> None:
    with pytest.raises(ValidationError):
        _SCHEDULE_ADAPTER.validate_python({"mode": "monthly"})


# ---------------------------------------------------------------------------
# IntentCreate timezone validation
# ---------------------------------------------------------------------------


def test_intent_create_valid_timezone() -> None:
    ic = IntentCreate(
        name="t",
        text="x",
        channels=VALID_CHANNELS,
        timezone="Asia/Shanghai",
    )
    assert ic.timezone == "Asia/Shanghai"


def test_intent_create_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        IntentCreate(
            name="t",
            text="x",
            channels=VALID_CHANNELS,
            timezone="Asia/Shanhai",  # intentional typo
        )


def test_intent_create_utc_timezone() -> None:
    ic = IntentCreate(name="t", text="x", channels=VALID_CHANNELS)
    assert ic.timezone == "UTC"


def test_intent_create_default_schedule_is_cron_daily() -> None:
    ic = IntentCreate(name="t", text="x", channels=VALID_CHANNELS)
    assert isinstance(ic.schedule, CronSchedule)
    assert ic.schedule.mode == "cron"
    assert ic.schedule.preset == "daily"


def test_intent_create_event_schedule() -> None:
    ic = IntentCreate(
        name="t",
        text="x",
        channels=VALID_CHANNELS,
        schedule={"mode": "event", "trigger_count": 2, "max_wait_seconds": 300},
    )
    assert isinstance(ic.schedule, EventSchedule)
    assert ic.schedule.trigger_count == 2

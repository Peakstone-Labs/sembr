"""Unit tests for DD1: Schedule discriminated union (IntervalSchedule / CronSchedule)."""
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from sembr.models import CronSchedule, IntentCreate, IntervalSchedule, Schedule

_SCHEDULE_ADAPTER: TypeAdapter[Schedule] = TypeAdapter(Schedule)

VALID_CHANNELS = [{"type": "email", "to": ["a@example.com"]}]


# ---------------------------------------------------------------------------
# IntervalSchedule
# ---------------------------------------------------------------------------


def test_interval_default_seconds() -> None:
    s = IntervalSchedule()
    assert s.mode == "interval"
    assert s.seconds == 3600


def test_interval_min_seconds() -> None:
    s = IntervalSchedule(seconds=60)
    assert s.seconds == 60


def test_interval_max_seconds() -> None:
    s = IntervalSchedule(seconds=604800)
    assert s.seconds == 604800


def test_interval_below_min_rejected() -> None:
    with pytest.raises(ValidationError):
        IntervalSchedule(seconds=59)


def test_interval_above_max_rejected() -> None:
    with pytest.raises(ValidationError):
        IntervalSchedule(seconds=604801)


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


# ---------------------------------------------------------------------------
# Discriminated union round-trip
# ---------------------------------------------------------------------------


def test_schedule_union_interval_roundtrip() -> None:
    raw = {"mode": "interval", "seconds": 7200}
    s = _SCHEDULE_ADAPTER.validate_python(raw)
    assert isinstance(s, IntervalSchedule)
    assert s.seconds == 7200
    dumped = s.model_dump()
    assert dumped == {"mode": "interval", "seconds": 7200}


def test_schedule_union_cron_roundtrip() -> None:
    raw = {"mode": "cron", "preset": "weekly", "weekday": "mon", "hour": 7, "minute": 0}
    s = _SCHEDULE_ADAPTER.validate_python(raw)
    assert isinstance(s, CronSchedule)
    assert s.preset == "weekly"
    assert s.weekday == "mon"


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

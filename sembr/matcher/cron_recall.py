# SPDX-License-Identifier: Apache-2.0
"""Enumerate past N scheduled fire-times for a CronSchedule.

APScheduler 3.x ``CronTrigger`` only walks forward (``get_next_fire_time``);
there is no ``get_prev_fire_time``.  Since ``CronSchedule`` is restricted to
three presets (``hourly`` / ``daily`` / ``weekly``) we reverse-engineer the
schedule arithmetically rather than pull in ``croniter`` — a single ~30-line
pure function with full unit-test coverage.

Semantics: the **current period's** fire-time is skipped.  A backfill triggered
at 12:30 on an hourly :15 schedule does NOT include 12:15, because the live
cron job either just fired or is about to fire at 12:15 — letting backfill
race the running scheduler would risk a duplicate write the UNIQUE index
catches but is still wasted LLM tokens.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sembr.models import CronSchedule

_WEEKDAY_TO_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


def past_n_fire_times(
    schedule: CronSchedule,
    tz: str,
    n: int,
    now: datetime | None = None,
) -> list[datetime]:
    """Return up to N UTC-aware past fire-times, newest first.

    The "current period" (hour for hourly, day for daily, week for weekly) is
    skipped — see module docstring for rationale.

    Best-effort for DST-affected timezones: a naive ``timedelta`` backstep may
    misalign with APScheduler's real fire by ≤1h on transition days.  Fixed-
    offset zones (UTC, ``Asia/Shanghai``, ``Asia/Tokyo``) have exact alignment;
    sembr's primary user base is in CN (UTC+8 no-DST), so this trade-off is
    accepted to avoid a new ``croniter`` dependency.
    """
    if n < 1:
        return []
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(tz)
    now_local = now.astimezone(zone)

    if schedule.preset == "hourly":
        hour_start = now_local.replace(minute=0, second=0, microsecond=0)
        f_newest = hour_start - timedelta(hours=1) + timedelta(minutes=schedule.minute)
        period = timedelta(hours=1)
    elif schedule.preset == "daily":
        day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        f_newest = (
            day_start - timedelta(days=1) + timedelta(hours=schedule.hour, minutes=schedule.minute)
        )
        period = timedelta(days=1)
    elif schedule.preset == "weekly":
        if schedule.weekday is None:
            raise ValueError("weekly preset requires weekday")
        wd_idx = _WEEKDAY_TO_INDEX[schedule.weekday]
        # Floor to this week's Monday 00:00 local, then step back one full week
        # and add the (weekday, hour, minute) offset.
        days_since_monday = now_local.weekday()
        week_start = (now_local - timedelta(days=days_since_monday)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        f_newest = (
            week_start
            - timedelta(days=7)
            + timedelta(days=wd_idx, hours=schedule.hour, minutes=schedule.minute)
        )
        period = timedelta(days=7)
    else:
        raise ValueError(f"unsupported preset: {schedule.preset!r}")

    return [(f_newest - i * period).astimezone(UTC) for i in range(n)]

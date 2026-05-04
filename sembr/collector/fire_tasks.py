"""In-memory feed fire task state store (D13).

Parallel to sembr.matcher.fire_tasks but scoped to feeds. Tasks are stored in a
module-level dict; TTL sweep runs every 5 minutes via APScheduler. Process-local,
not persisted — same trade-off as intent fire tasks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)

_TASK_TTL_SECONDS = 3600  # 1 hour — consistent with intent fire tasks
_FIRE_RATE_LIMIT_SECONDS = 60  # D9: 1 real fire per feed per 60s


@dataclass
class FeedFireTask:
    task_id: str
    feed_id: int
    dry_run: bool
    status: str  # "running" | "done" | "error"
    started_at: datetime
    finished_at: datetime | None = None
    articles_seen: int = 0
    articles_new: int = 0
    articles: list[dict] = field(default_factory=list)
    error: str | None = None
    _created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_feed_fire_tasks: dict[str, FeedFireTask] = {}

# Last REAL fire time per feed_id (dry_run fires do not update this — D8)
_last_fire_at: dict[int, datetime] = {}


def create_task(feed_id: int, dry_run: bool) -> FeedFireTask:
    task = FeedFireTask(
        task_id=str(uuid4()),
        feed_id=feed_id,
        dry_run=dry_run,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    _feed_fire_tasks[task.task_id] = task
    if not dry_run:
        # D8: only real runs consume the rate-limit bucket
        _last_fire_at[feed_id] = datetime.now(timezone.utc)
    return task


def get_task(task_id: str) -> FeedFireTask | None:
    return _feed_fire_tasks.get(task_id)


def throttle_check(feed_id: int, rate_seconds: int = _FIRE_RATE_LIMIT_SECONDS) -> bool:
    """Return True if a real fire is allowed. D8: dry_run bypasses this check entirely."""
    last = _last_fire_at.get(feed_id)
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= rate_seconds


def sweep_expired(ttl_seconds: int = _TASK_TTL_SECONDS) -> int:
    """Remove tasks older than ttl_seconds. APScheduler calls this every 5 minutes."""
    now = datetime.now(timezone.utc)
    to_remove = [
        k for k, v in _feed_fire_tasks.items()
        if (now - v._created_at).total_seconds() > ttl_seconds
    ]
    for k in to_remove:
        del _feed_fire_tasks[k]
    if to_remove:
        logger.debug("feed fire sweep_expired: removed %d expired tasks", len(to_remove))
    return len(to_remove)


def _reset_for_testing() -> None:
    """Clear all tasks and rate-limit state — test helper only."""
    _feed_fire_tasks.clear()
    _last_fire_at.clear()

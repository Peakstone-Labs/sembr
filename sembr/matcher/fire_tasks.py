# SPDX-License-Identifier: Apache-2.0
"""In-memory fire task state store.

Tasks are stored in a module-level dict. TTL sweep runs every 5 minutes
via APScheduler, keeping memory bounded to ~1h of recent fire results.
sembr is single-process / single-instance, so no shared-state concerns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from uuid import uuid4

logger = logging.getLogger(__name__)

_TASK_TTL_SECONDS = 3600  # 1 hour
_FIRE_RATE_LIMIT_SECONDS = 60  # 1 fire per intent per minute


@dataclass
class FireTask:
    task_id: str
    intent_id: int
    status: str  # "running" | "done" | "error"
    started_at: datetime
    finished_at: datetime | None = None
    match_count: int = 0
    matches: list[dict] = field(default_factory=list)
    pushed: bool = False
    push_error: str | None = None  # always None — pipeline.handle never-raise; reserved for future
    _created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Module-level dict; reset between tests via _reset_for_testing()
_fire_tasks: dict[str, FireTask] = {}

# Last fire time per intent_id for rate limiting
_last_fire_at: dict[int, datetime] = {}


def create_task(intent_id: int) -> FireTask:
    task = FireTask(
        task_id=str(uuid4()),
        intent_id=intent_id,
        status="running",
        started_at=datetime.now(timezone.utc),
    )
    _fire_tasks[task.task_id] = task
    _last_fire_at[intent_id] = datetime.now(timezone.utc)
    return task


def get_task(task_id: str) -> FireTask | None:
    return _fire_tasks.get(task_id)


def throttle_check(intent_id: int, rate_seconds: int = _FIRE_RATE_LIMIT_SECONDS) -> bool:
    """Return True if the fire is allowed (not rate-limited). False = reject with 429.

    Records are NOT written here — create_task() records the fire time so the
    clock only starts on an accepted request, not a rejected one.
    """
    last = _last_fire_at.get(intent_id)
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last).total_seconds() >= rate_seconds


def check_and_record_fire(intent_id: int, rate_seconds: int = _FIRE_RATE_LIMIT_SECONDS) -> bool:
    """Atomic check-and-record for the sync external fire path (D-A9).

    Returns True (and writes ``_last_fire_at[intent_id]``) when the fire is
    allowed; False otherwise. Stays a *synchronous* function on purpose:
    asyncio's single-threaded scheduler cannot interleave two callers between
    the read and the write as long as no ``await`` sits between them. Adding
    any ``async``/``await`` here would reopen the TOCTOU window the original
    throttle_check + create_task pair already exhibits in concept.

    Independent from ``create_task`` (which is the async path's record point);
    sharing ``_last_fire_at`` means the two paths form a single 1/intent/60s
    bucket — see external-fire-api design D-A5.
    """
    last = _last_fire_at.get(intent_id)
    now = datetime.now(timezone.utc)
    if last is not None and (now - last).total_seconds() < rate_seconds:
        return False
    _last_fire_at[intent_id] = now
    return True


def sweep_expired(ttl_seconds: int = _TASK_TTL_SECONDS) -> int:
    """Remove tasks older than ttl_seconds. APScheduler calls this every 5 minutes."""
    now = datetime.now(timezone.utc)
    to_remove = [
        k for k, v in _fire_tasks.items() if (now - v._created_at).total_seconds() > ttl_seconds
    ]
    for k in to_remove:
        del _fire_tasks[k]
    if to_remove:
        logger.debug("sweep_expired: removed %d expired fire tasks", len(to_remove))
    return len(to_remove)


def _reset_for_testing() -> None:
    """Clear all tasks and rate-limit state — test helper only."""
    _fire_tasks.clear()
    _last_fire_at.clear()

"""In-memory ManualPruneTask state store + 5-minute sweep.

Mirrors the ``feeds_fire`` pattern (``sembr/collector/fire_tasks.py``):
process-local dict keyed by uuid4 task_id, swept on a periodic APScheduler
job. Task state is intentionally not persisted — same trade-off as feed/intent
fire tasks. State machine: planning → planned → applying → done | error.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

logger = logging.getLogger(__name__)

# Tasks linger 5 minutes past creation so a slow client can still poll the
# `done` payload. Shorter than feed-fire's 1h because manual prune is an
# interactive flow — there is no "fire and forget" use case.
_TASK_TTL_SECONDS = 300

ManualPruneStatus = Literal["planning", "planned", "applying", "done", "error"]
ManualPruneTarget = Literal["news", "dead"]


@dataclass
class ManualPruneTask:
    task_id: str
    target: ManualPruneTarget
    feed_ids: list[int]
    older_than_days: int
    status: ManualPruneStatus
    started_at: datetime
    finished_at: datetime | None = None
    plan_summary: dict | None = None
    result_summary: dict | None = None
    error: str | None = None
    _created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


_manual_prune_tasks: dict[str, ManualPruneTask] = {}

# Status values that mean the task has stopped progressing — only these are
# eligible for sweep. In-flight tasks (planning / applying) must survive any
# number of TTL ticks, otherwise a user who wandered off after the dry-run
# (or a 4-minute large apply on 140k rows) gets a phantom 404 mid-poll.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "error"})


def create_task(
    target: ManualPruneTarget,
    feed_ids: list[int],
    older_than_days: int,
) -> ManualPruneTask:
    task = ManualPruneTask(
        task_id=str(uuid4()),
        target=target,
        feed_ids=list(feed_ids),
        older_than_days=older_than_days,
        status="planning",
        started_at=datetime.now(timezone.utc),
    )
    _manual_prune_tasks[task.task_id] = task
    return task


def get_task(task_id: str) -> ManualPruneTask | None:
    return _manual_prune_tasks.get(task_id)


def sweep_expired(ttl_seconds: int = _TASK_TTL_SECONDS) -> int:
    """Drop terminal tasks whose ``finished_at`` (or, as a fallback,
    ``_created_at``) is older than ``ttl_seconds``.

    Skips non-terminal (planning / applying) tasks regardless of age — a slow
    140k-row apply can run for several minutes and the user must be able to
    keep polling without hitting a phantom 404.
    """
    now = datetime.now(timezone.utc)
    to_remove: list[str] = []
    for k, v in _manual_prune_tasks.items():
        if v.status not in _TERMINAL_STATUSES:
            continue
        anchor = v.finished_at or v._created_at
        if (now - anchor).total_seconds() > ttl_seconds:
            to_remove.append(k)
    for k in to_remove:
        del _manual_prune_tasks[k]
    if to_remove:
        logger.debug(
            "manual_prune sweep_expired: removed %d expired tasks", len(to_remove)
        )
    return len(to_remove)


def _reset_for_testing() -> None:
    """Clear all tasks — test helper only."""
    _manual_prune_tasks.clear()

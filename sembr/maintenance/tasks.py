# SPDX-License-Identifier: Apache-2.0
"""In-memory ManualPruneTask state store + 5-minute sweep.

Mirrors the ``feeds_fire`` pattern (``sembr/collector/fire_tasks.py``):
process-local dict keyed by uuid4 task_id, swept on a periodic APScheduler
job. Task state is intentionally not persisted — same trade-off as feed/intent
fire tasks. State machine: planning → planned → applying → done | error.
"""

from __future__ import annotations

import logging
import typing
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
    _created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_manual_prune_tasks: dict[str, ManualPruneTask] = {}

# Statuses where the task is still in flight as far as the user is concerned
# — sweep must skip these regardless of age:
#   - planning : background task is computing the dry-run plan
#   - planned  : dry-run done, user is reading the per-feed numbers and
#                hasn't clicked Confirm yet (could be away from the screen)
#   - applying : real delete in progress (140k apply can take minutes)
# A user who wandered off after dry-run, or a slow apply, must NOT see a
# phantom 404 mid-poll because sweep cleared their task entry.
_NON_TERMINAL_STATUSES: frozenset[str] = frozenset({"planning", "planned", "applying"})

# Pin terminal statuses explicitly so future Literal additions can't silently
# slide into either partition; the assert tying both sets back to ManualPruneStatus
# fails fast at import time if anything drifts out of sync.
_TERMINAL_STATUSES: frozenset[str] = frozenset({"done", "error"})

_ALL_STATUSES: frozenset[str] = frozenset(typing.get_args(ManualPruneStatus))
assert _ALL_STATUSES == _TERMINAL_STATUSES | _NON_TERMINAL_STATUSES, (
    "ManualPruneStatus literal vs sweep partition out of sync: "
    f"all={_ALL_STATUSES} terminal={_TERMINAL_STATUSES} "
    f"non_terminal={_NON_TERMINAL_STATUSES}"
)
assert not (_TERMINAL_STATUSES & _NON_TERMINAL_STATUSES), (
    "_TERMINAL_STATUSES and _NON_TERMINAL_STATUSES must be disjoint"
)


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
        logger.debug("manual_prune sweep_expired: removed %d expired tasks", len(to_remove))
    return len(to_remove)


def _reset_for_testing() -> None:
    """Clear all tasks — test helper only."""
    _manual_prune_tasks.clear()

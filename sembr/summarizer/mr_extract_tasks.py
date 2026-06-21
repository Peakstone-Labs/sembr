# SPDX-License-Identifier: Apache-2.0
"""In-memory state + per-digest locks for the map sub-feature's extract task.

Mirrors ``matcher/backfill_tasks.py``: a process-local dict scoped to a single
sembr instance, swept on a 24h TTL (extraction over a full digest's citations can
take a minute and the user expects to inspect the result later).

The concurrency gate is keyed on ``row_id`` (``summary_history.id``, globally
unique) so two *different* digests extract concurrently, but a second
"sources extraction" on the *same* digest is rejected with 409 instead of
double-spending LLM calls.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

logger = logging.getLogger(__name__)

_EXTRACT_TASK_TTL_SECONDS = 24 * 3600  # 24h — keep results inspectable


@dataclass
class ExtractProgress:
    done: int = 0  # newly extracted (or override-overwritten) this run
    skipped: int = 0  # cache hit, override off
    errors: int = 0  # per-article failures (expired body / LLM / validation)
    total: int = 0  # citations in the digest


@dataclass
class ExtractTask:
    task_id: str
    intent_id: int
    row_id: int
    status: str  # "running" | "done" | "error"
    started_at: datetime
    progress: ExtractProgress
    errors: list[dict] = field(default_factory=list)  # [{article_id, reason}]
    finished_at: datetime | None = None
    error_reason: str | None = None  # whole-task failure (not per-article)
    _created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_extract_tasks: dict[str, ExtractTask] = {}
_row_locks: dict[int, asyncio.Lock] = {}


def get_row_lock(row_id: int) -> asyncio.Lock:
    lock = _row_locks.get(row_id)
    if lock is None:
        lock = asyncio.Lock()
        _row_locks[row_id] = lock
    return lock


def try_acquire_row(row_id: int) -> bool:
    """Synchronously try to take the per-digest gate. See backfill_tasks for the
    no-TOCTOU rationale (sync flip, no event-loop yield between check and set)."""
    lock = get_row_lock(row_id)
    if lock.locked():
        return False
    lock._locked = True  # noqa: SLF001
    return True


def release_row(row_id: int) -> None:
    """Release the per-digest gate. Idempotent on already-released."""
    lock = _row_locks.get(row_id)
    if lock is not None and lock.locked():
        lock.release()


def forget_row_lock(row_id: int) -> None:
    """Drop the lock entry; no-op while held. Keeps the dict from growing."""
    lock = _row_locks.get(row_id)
    if lock is None or lock.locked():
        return
    _row_locks.pop(row_id, None)


def create_task(intent_id: int, row_id: int, total: int) -> ExtractTask:
    task = ExtractTask(
        task_id=str(uuid4()),
        intent_id=intent_id,
        row_id=row_id,
        status="running",
        started_at=datetime.now(UTC),
        progress=ExtractProgress(total=total),
    )
    _extract_tasks[task.task_id] = task
    return task


def get_task(task_id: str) -> ExtractTask | None:
    return _extract_tasks.get(task_id)


def sweep_expired(ttl_seconds: int = _EXTRACT_TASK_TTL_SECONDS) -> int:
    """Remove tasks older than ``ttl_seconds``. APScheduler calls this every 5 min."""
    now = datetime.now(UTC)
    to_remove = [
        k for k, v in _extract_tasks.items() if (now - v._created_at).total_seconds() > ttl_seconds
    ]
    for k in to_remove:
        del _extract_tasks[k]
    if to_remove:
        logger.debug("sweep_expired: removed %d expired extract tasks", len(to_remove))
    return len(to_remove)


def _reset_for_testing() -> None:
    _extract_tasks.clear()
    _row_locks.clear()

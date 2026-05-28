# SPDX-License-Identifier: Apache-2.0
"""In-memory backfill task state + per-intent locks.

Mirrors ``matcher/fire_tasks.py``: a process-local dict scoped to a single
sembr instance.  TTL is 24h (vs fire's 1h) — backfill can run for many minutes
and a user who leaves the browser still expects to come back later and inspect
the result.

The per-intent ``asyncio.Lock`` is the in-process mutual-exclusion gate.
``POST /intents/{id}/backfill`` tries the lock; an already-held lock yields
409 ``backfill_in_progress`` instead of queueing a second task.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

logger = logging.getLogger(__name__)

_BACKFILL_TASK_TTL_SECONDS = 24 * 3600  # 24h — backfill can run long


@dataclass
class BackfillProgress:
    done: int = 0
    skipped: int = 0
    empty_runs: int = 0
    total: int = 0


@dataclass
class BackfillTask:
    task_id: str
    intent_id: int
    status: str  # "running" | "done" | "error"
    started_at: datetime
    progress: BackfillProgress
    finished_at: datetime | None = None
    error_reason: str | None = None
    _created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


_backfill_tasks: dict[str, BackfillTask] = {}
_intent_locks: dict[int, asyncio.Lock] = {}


def get_intent_lock(intent_id: int) -> asyncio.Lock:
    """Return the (lazily created) per-intent backfill concurrency gate."""
    lock = _intent_locks.get(intent_id)
    if lock is None:
        lock = asyncio.Lock()
        _intent_locks[intent_id] = lock
    return lock


def try_acquire_intent(intent_id: int) -> bool:
    """Synchronously try to acquire the per-intent backfill gate.

    Returns ``True`` if the lock was acquired, ``False`` if another backfill is
    already holding it.  Mirrors ``threading.Lock.acquire(blocking=False)``
    semantics for ``asyncio.Lock`` — avoids the fragile
    ``lock.locked() / await lock.acquire()`` pattern that depends on CPython's
    fast-path implementation detail (acquire-on-unlocked returns without
    yielding to the event loop).

    Synchronous on purpose: an event-loop yield between the check and the
    flip would reopen the TOCTOU window.  By staying sync we guarantee no
    interleaving under cooperative scheduling.

    The caller (POST handler) takes the lock; the background orchestrator
    releases it via :func:`release_intent` in a ``finally`` block.

    .. note:: This is a single-process lock — multi-worker uvicorn deployments
       must serialise backfill requests at the reverse-proxy layer (sticky
       routing) or accept that two workers can each run a backfill for the
       same intent concurrently (only the UNIQUE(intent_id, run_at) DB index
       prevents duplicate writes; LLM calls and notifications will double).
    """
    lock = get_intent_lock(intent_id)
    if lock.locked():
        return False
    # Set the private flag — equivalent to the CPython fast-path of
    # acquire() on an unlocked lock, but explicit so future asyncio changes
    # cannot silently break our concurrency contract.
    lock._locked = True  # noqa: SLF001
    return True


def release_intent(intent_id: int) -> None:
    """Release the per-intent backfill gate.  Idempotent on already-released."""
    lock = _intent_locks.get(intent_id)
    if lock is not None and lock.locked():
        lock.release()


def forget_intent_lock(intent_id: int) -> None:
    """Drop the per-intent lock entry when its intent is deleted.

    Prevents the dict from growing unboundedly across create/delete churn.
    No-op if the lock is currently held (DELETE while backfill in flight is
    a separate race — the orchestrator's mid-run ``get_intent`` re-check will
    exit cleanly and release).
    """
    lock = _intent_locks.get(intent_id)
    if lock is None:
        return
    if lock.locked():
        return
    _intent_locks.pop(intent_id, None)


def create_task(intent_id: int, total: int) -> BackfillTask:
    task = BackfillTask(
        task_id=str(uuid4()),
        intent_id=intent_id,
        status="running",
        started_at=datetime.now(UTC),
        progress=BackfillProgress(total=total),
    )
    _backfill_tasks[task.task_id] = task
    return task


def get_task(task_id: str) -> BackfillTask | None:
    return _backfill_tasks.get(task_id)


def sweep_expired(ttl_seconds: int = _BACKFILL_TASK_TTL_SECONDS) -> int:
    """Remove tasks older than ``ttl_seconds``.  APScheduler calls this every 5 min."""
    now = datetime.now(UTC)
    to_remove = [
        k for k, v in _backfill_tasks.items() if (now - v._created_at).total_seconds() > ttl_seconds
    ]
    for k in to_remove:
        del _backfill_tasks[k]
    if to_remove:
        logger.debug("sweep_expired: removed %d expired backfill tasks", len(to_remove))
    return len(to_remove)


def _reset_for_testing() -> None:
    _backfill_tasks.clear()
    _intent_locks.clear()

# SPDX-License-Identifier: Apache-2.0
"""Self-healing backfill for the derived payload fields on ``news_current``.

``/api/news/search`` applies one filter surface across ``news_current`` and
``news_archive``. Ingest stamps the four derived keys from now on
(``embedder/scheduler.py::_to_point``), but every point written before that
change has none of them — and a ``Range`` / ``Match`` filter never matches an
absent field, so those points would silently drop out of every derived-field
query. Silent under-recall is the one failure this feature exists to prevent.

A background job rather than a one-shot ops script: this is a public repo, and
every deployment's ``news_current`` has the same gap. A runbook reaches this
box; a job reaches everyone.

The queue is self-consuming. ``build_derived_payload`` always writes
``body_len``, so ``IsEmpty("body_len")`` shrinks by exactly the points just
processed — the job re-scrolls from the head every batch instead of paging
with an offset, which would be meaningless over a set that shrinks underneath
it. Progress is therefore monotonic, and the run is idempotent and safe to
interrupt at any point.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from sembr.vector_store.derived_fields import build_derived_payload
from sembr.vector_store.news import ALIAS_NAME

if TYPE_CHECKING:
    from sembr.vector_store.qdrant import QdrantHandle

logger = logging.getLogger(__name__)

JOB_ID = "maintenance_news_derived_backfill"

# The field whose presence means "this point has been processed". It must be
# one `build_derived_payload` writes unconditionally, or a point with an
# unparseable timestamp / URL would never leave the queue.
_PROGRESS_FIELD = "body_len"

# 500 per batch: payload-only reads and writes (no 1024-dim vectors), so this
# is an order of magnitude lighter than the archive migration's 250.
_SCROLL_BATCH = 500

# Yield between batches so the matcher / ingest path is never queued behind a
# long backfill run inside the same event loop.
_BATCH_PAUSE_SECONDS = 0.2

# One round gets 10 minutes; leftovers wait for the next interval. Bounds the
# job's share of the container regardless of how far behind the collection is.
_WALL_CLOCK_BUDGET_SECONDS = 600

# 30 minutes, deliberately NOT `maintenance_interval_hours` (24h). A round
# truncated by the wall-clock budget must resume soon: while the queue is
# non-empty, derived-field filters under-recall and every search that uses one
# carries a warning. At 30 min the ~113k-point production backlog converges
# inside the first round anyway; at 24h a single truncation would stretch that
# warning window into days.
_INTERVAL_MINUTES = 30

# 2 minutes after startup: late enough that lifespan's collection bootstrap
# (which creates the `body_len` index this job's own queue query needs) has
# long finished, early enough that a fresh deployment converges within the
# first half hour.
_FIRST_RUN_DELAY_MINUTES = 2

# Consecutive rounds a single batch may make zero progress before its ids are
# skipped. A point whose payload Qdrant refuses to update would otherwise sit
# at the head of the queue and burn every subsequent round's budget.
_STALL_ROUNDS_BEFORE_QUARANTINE = 3

# Once points are quarantined the condition is permanent until someone looks;
# report it daily instead of every 30 minutes.
_QUARANTINE_LOG_INTERVAL_SECONDS = 24 * 3600


class BackfillState:
    """Cross-round memory for one registered job (process-local, not durable).

    Losing it on restart is harmless: a restarted process simply re-discovers
    a stalled batch over the next three rounds.
    """

    def __init__(self) -> None:
        self.quarantined: set[str] = set()
        self._stalled_ids: frozenset[str] = frozenset()
        self._stall_rounds = 0
        self._last_quarantine_log: float | None = None

    def note_stall(self, batch_ids: list[str]) -> bool:
        """Record a zero-progress batch. Returns True if it was just quarantined.

        The streak only counts CONSECUTIVE rounds stalling on the SAME batch —
        a different batch stalling means the queue moved, and a fresh count is
        the honest reading.
        """
        key = frozenset(batch_ids)
        if key == self._stalled_ids:
            self._stall_rounds += 1
        else:
            self._stalled_ids = key
            self._stall_rounds = 1
        if self._stall_rounds >= _STALL_ROUNDS_BEFORE_QUARANTINE:
            self.quarantined |= key
            self._stalled_ids = frozenset()
            self._stall_rounds = 0
            return True
        return False

    def note_progress(self) -> None:
        self._stalled_ids = frozenset()
        self._stall_rounds = 0


def _pending_filter(exclude_ids: set[str] | None = None) -> Any:
    from qdrant_client.models import (  # noqa: PLC0415
        Filter,
        HasIdCondition,
        IsEmptyCondition,
        PayloadField,
    )

    must = [IsEmptyCondition(is_empty=PayloadField(key=_PROGRESS_FIELD))]
    must_not = [HasIdCondition(has_id=sorted(exclude_ids))] if exclude_ids else None
    return Filter(must=must, must_not=must_not)


async def count_pending_derived(client: Any, *, exclude_ids: set[str] | None = None) -> int:
    """Points in ``news_current`` still missing their derived fields.

    Recomputed from Qdrant on every call rather than read back from the job's
    own bookkeeping — "the backfill says it finished" and "no point is missing
    the field" are two different claims, and only the second one is the
    acceptance criterion.
    """
    result = await client.count(
        collection_name=ALIAS_NAME,
        count_filter=_pending_filter(exclude_ids),
        exact=True,
    )
    return result.count


def set_pending_flag(app: Any, pending: int | None) -> None:
    """Publish the queue depth for the search endpoint's degradation warning.

    ``None`` means "could not determine", which callers must treat exactly
    like a non-zero count: the alternative is answering a derived-field query
    with a short result set and no indication that it is short.
    """
    if app is None:
        return
    app.state.news_derived_backfill_pending = pending


async def initialise_pending_flag(app: Any, qdrant_handle: QdrantHandle) -> None:
    """Give the flag a value during lifespan, before the first job round.

    Without this the window between process start and the job's first fire
    (2 minutes, or forever if job registration failed) would serve
    derived-field queries against an unbackfilled collection with no warning —
    which is precisely the silent under-recall the warning exists to prevent.
    """
    try:
        pending = await count_pending_derived(qdrant_handle.client)
    except Exception:
        logger.warning(
            "news derived backfill: initial pending count failed; "
            "search will warn until the first job round succeeds",
            exc_info=True,
        )
        set_pending_flag(app, None)
        return
    set_pending_flag(app, pending)
    logger.info("news derived backfill: %d point(s) pending at startup", pending)


def _maybe_log_quarantine(state: BackfillState) -> None:
    if not state.quarantined:
        return
    now = monotonic()
    if (
        state._last_quarantine_log is not None
        and now - state._last_quarantine_log < _QUARANTINE_LOG_INTERVAL_SECONDS
    ):
        return
    state._last_quarantine_log = now
    logger.error(
        "news derived backfill: %d point(s) quarantined after %d rounds of zero "
        "progress and are skipped; they stay invisible to derived-field filters "
        "until inspected (ids: %s)",
        len(state.quarantined),
        _STALL_ROUNDS_BEFORE_QUARANTINE,
        sorted(state.quarantined)[:10],
    )


async def _run_news_derived_backfill(
    qdrant_handle: QdrantHandle,
    state: BackfillState,
    app: Any = None,
) -> None:
    """One round: drain the queue until it is empty, stalls, or time runs out."""
    started_at = monotonic()
    client = qdrant_handle.client
    batches = 0
    updated = 0
    outcome = "converged"

    try:
        queue = await count_pending_derived(client, exclude_ids=state.quarantined)
    except Exception:
        logger.warning("news derived backfill: pending count failed", exc_info=True)
        set_pending_flag(app, None)
        return

    try:
        while queue > 0:
            if monotonic() - started_at >= _WALL_CLOCK_BUDGET_SECONDS:
                outcome = "budget_exhausted"
                break

            points, _next_unused = await client.scroll(
                collection_name=ALIAS_NAME,
                scroll_filter=_pending_filter(state.quarantined),
                with_payload=True,
                with_vectors=False,
                limit=_SCROLL_BATCH,
            )
            if not points:
                # The count and the scroll disagree (concurrent TTL delete, or
                # a count that lagged); trust the scroll and stop.
                queue = 0
                break

            from qdrant_client.models import SetPayload, SetPayloadOperation  # noqa: PLC0415

            operations = [
                SetPayloadOperation(
                    set_payload=SetPayload(
                        # set_payload MERGES keys; the seven base payload
                        # fields written at ingest are left untouched.
                        payload=build_derived_payload(p.payload or {}),
                        points=[str(p.id)],
                    )
                )
                for p in points
            ]
            await client.batch_update_points(
                collection_name=ALIAS_NAME,
                update_operations=operations,
                wait=True,
            )
            batches += 1

            remaining = await count_pending_derived(client, exclude_ids=state.quarantined)
            if remaining >= queue:
                # The batch was written and the queue did not shrink, so those
                # points are not leaving it. Retrying the same batch for the
                # rest of the budget would accomplish nothing.
                outcome = "zero_progress"
                batch_ids = [str(p.id) for p in points]
                if state.note_stall(batch_ids):
                    _maybe_log_quarantine(state)
                else:
                    logger.warning(
                        "news derived backfill: batch of %d made no progress "
                        "(queue stayed at %d); aborting this round",
                        len(points),
                        remaining,
                    )
                queue = remaining
                break

            state.note_progress()
            updated += queue - remaining
            queue = remaining
            await asyncio.sleep(_BATCH_PAUSE_SECONDS)
    except Exception:
        outcome = "error"
        logger.warning("news derived backfill: run aborted", exc_info=True)

    _maybe_log_quarantine(state)

    # The published flag is the RAW queue depth — quarantined points really are
    # missing their fields, so hiding them from the flag would hide a real
    # under-recall from every caller.
    try:
        pending = queue if not state.quarantined else await count_pending_derived(client)
    except Exception:
        logger.warning("news derived backfill: final pending count failed", exc_info=True)
        pending = None
    set_pending_flag(app, pending)

    logger.info(
        "news derived backfill run: outcome=%s batches=%d updated=%d pending=%s "
        "quarantined=%d elapsed_ms=%d",
        outcome,
        batches,
        updated,
        pending,
        len(state.quarantined),
        int((monotonic() - started_at) * 1000),
    )


def add_news_derived_backfill_job(
    scheduler: AsyncIOScheduler,
    qdrant_handle: QdrantHandle,
    app: Any = None,
) -> BackfillState:
    """Register the backfill job. Returns its state so tests can inspect it."""
    state = BackfillState()
    now = datetime.now(UTC)
    scheduler.add_job(
        _run_news_derived_backfill,
        trigger=IntervalTrigger(
            minutes=_INTERVAL_MINUTES,
            start_date=now + timedelta(minutes=_FIRST_RUN_DELAY_MINUTES),
        ),
        id=JOB_ID,
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        args=[qdrant_handle, state, app],
    )
    return state

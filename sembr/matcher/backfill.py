# SPDX-License-Identifier: Apache-2.0
"""Backfill orchestrator: replay past N scheduled fire-times for a cron intent.

The orchestrator deliberately bypasses ``SummaryPipeline.handle`` (which calls
``on_summary`` / ``on_persist`` and would re-fan-out notifications) — it calls
the public ``compute_summary`` directly and writes via ``save_summary_or_skip``
itself.  This keeps the live single-instance pipeline's per-tick callbacks
intact while letting backfill produce per-fire-time summary rows.

Concurrency model: one ``asyncio.Lock`` per intent_id (in
``matcher.backfill_tasks``).  The handler tries the lock with ``acquire(False)``;
no-contention → spawn the background task, take the lock for the whole run, and
release in ``finally``.  A second concurrent POST is rejected with 409.

Crash safety: the orchestrator pauses the matcher job at start, resumes it in
``finally``.  ``register_all_enabled`` at startup also resumes every
``matcher-intent-*`` job as a safety-net, so an OOM'd backfill won't silence the
intent forever (see ``matcher.jobs.register_all_enabled``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.jobstores.base import JobLookupError

from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.db.summary_history import save_summary_or_skip
from sembr.matcher.backfill_tasks import BackfillTask, release_intent
from sembr.matcher.cron_recall import past_n_fire_times
from sembr.matcher.scan import ScanOptions, scan_once
from sembr.models import CronSchedule
from sembr.vector_store.news import ALIAS_NAME as _NEWS_ALIAS

logger = logging.getLogger(__name__)

# Per-iteration throttle.  SiliconFlow has no documented hard QPS, but a tight
# loop over 300 hourly runs can easily burst above any provider's burst quota;
# 200 ms is enough headroom for backfill to coexist with normal cron / fire
# without elevating LLM 429 risk.
_INTER_RUN_SLEEP_SECONDS = 0.2

# Format mirrors save_summary's ``%Y-%m-%dT%H:%M:%SZ``.  Must stay in sync so
# the UNIQUE(intent_id, run_at) index keyed on backfill writes correctly
# collides with future normal-cron writes at the same fire-time.
_RUN_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


async def probe_oldest_news_ts(qdrant_client) -> int | None:
    """Return the global minimum ``ingested_at_ts`` in news_current (epoch seconds).

    Uses ``scroll(order_by=asc, limit=1)`` against the payload-indexed
    ``ingested_at_ts`` field — O(1) and cheap.  Returns ``None`` when the
    collection is empty.

    **Known limitation**: collector writing an anomalous timestamp (e.g. epoch
    0 from a broken feed) causes this probe to overestimate effective coverage.
    1.0 accepts the risk as low-probability; a future iteration could group
    by feed_id and take a per-feed minimum.
    """
    from qdrant_client.models import OrderBy  # noqa: PLC0415

    try:
        points, _ = await qdrant_client.scroll(
            collection_name=_NEWS_ALIAS,
            limit=1,
            with_payload=["ingested_at_ts"],
            with_vectors=False,
            order_by=OrderBy(key="ingested_at_ts", direction="asc"),
        )
    except Exception as exc:
        logger.warning("probe_oldest_news_ts: scroll failed: %s", exc)
        return None
    if not points:
        return None
    payload = points[0].payload or {}
    ts = payload.get("ingested_at_ts")
    if not isinstance(ts, int):
        return None
    return ts


async def run_backfill(
    intent_id: int,
    past_runs: int,
    app,
    task: BackfillTask,
) -> None:
    """Background orchestrator — drives one BackfillTask to completion.

    **LOCK OWNERSHIP CONTRACT**

    - The caller (``sembr.api.history.post_backfill``) MUST have already
      acquired the per-intent lock via ``try_acquire_intent(intent_id)``
      before spawning this coroutine.  This function never tries to take
      the lock itself — doing so would deadlock.
    - This function ALWAYS releases that lock in its ``finally`` block,
      even on exception or ``CancelledError``.  Callers must NOT release
      it themselves once spawn has succeeded.
    - The scheduler's ``matcher-intent-{id}`` job is paused at the top of
      ``try`` and resumed in ``finally`` symmetrically.  ``finally`` swallows
      both ``JobLookupError`` (intent DELETE'd mid-run cascaded the job) and
      any other exception from ``resume_job`` (e.g. ``SchedulerNotRunningError``
      during process shutdown) — never let cleanup raise and mask the
      original exception.
    """
    scheduler = app.state.scheduler
    qdrant_client = app.state.qdrant.client
    pipeline = app.state.summary_pipeline
    conn = get_conn()
    job_id = f"matcher-intent-{intent_id}"

    try:
        # Snapshot intent up front; mid-run PUTs must not mutate the schedule
        # we replay against.
        snapshot = await get_intent(conn, intent_id)
        if snapshot is None or not isinstance(snapshot.schedule, CronSchedule):
            task.status = "error"
            task.error_reason = "intent_invalid"
            task.finished_at = datetime.now(UTC)
            return

        # Pause normal cron while we replay; finally block resumes.
        try:
            scheduler.pause_job(job_id)
        except JobLookupError:
            # Job may not exist if intent was created disabled. Backfill still
            # proceeds; resume in finally will also no-op.
            logger.debug("backfill: matcher job %s absent at pause; continuing", job_id)

        fire_times = past_n_fire_times(snapshot.schedule, snapshot.timezone, past_runs)
        # past_n_fire_times returns newest-first; replay oldest-first so the
        # {history} prompt slot for each iteration sees only earlier replays.
        fire_times.reverse()

        # Defence-in-depth: handler's 422 pre-check already enforces this,
        # so this second probe is intentionally redundant — guards against
        # operators invoking run_backfill directly (tests, REPL, future
        # internal callers) without going through the HTTP layer.  Cost is
        # one extra Qdrant scroll per backfill, which is cheap.
        oldest_news_ts = await probe_oldest_news_ts(qdrant_client)
        if oldest_news_ts is not None and fire_times:
            earliest_target = fire_times[0]
            if earliest_target.timestamp() < oldest_news_ts:
                task.status = "error"
                task.error_reason = "qdrant_depth"
                task.finished_at = datetime.now(UTC)
                return

        for past_fire_time in fire_times:
            # Re-check intent existence — N=300 hourly backfills can run for
            # tens of minutes; a mid-run DELETE must exit cleanly before
            # touching Qdrant / LLM.
            current = await get_intent(conn, intent_id)
            if current is None:
                task.status = "error"
                task.error_reason = "intent_deleted_mid_run"
                task.finished_at = datetime.now(UTC)
                return

            options = ScanOptions(
                lookback_seconds=snapshot.schedule.lookback_seconds,
                threshold=snapshot.threshold,
                skip_seen=snapshot.schedule.skip_seen,
                feed_ids=snapshot.feed_filter.ids if snapshot.feed_filter else None,
                write_match_seen=True,
                propagate_qdrant_errors=False,
                now=past_fire_time,
            )
            try:
                matches = await scan_once(snapshot, options, conn, qdrant_client)
            except Exception as exc:
                logger.warning(
                    "backfill: scan_once failed at run_at=%s for intent=%d: %s",
                    past_fire_time,
                    intent_id,
                    exc,
                )
                matches = []

            if not matches:
                task.progress.empty_runs += 1
                await asyncio.sleep(_INTER_RUN_SLEEP_SECONDS)
                continue

            try:
                result = await pipeline.compute_summary(matches, now=past_fire_time)
            except Exception as exc:
                logger.warning(
                    "backfill: compute_summary failed at run_at=%s for intent=%d: %s",
                    past_fire_time,
                    intent_id,
                    exc,
                )
                result = None

            if result is None:
                await asyncio.sleep(_INTER_RUN_SLEEP_SECONDS)
                continue

            run_at_str = past_fire_time.strftime(_RUN_AT_FORMAT)
            inserted = await save_summary_or_skip(conn, result, run_at=run_at_str)
            if inserted:
                task.progress.done += 1
            else:
                task.progress.skipped += 1
            await asyncio.sleep(_INTER_RUN_SLEEP_SECONDS)

        if task.status == "running":
            task.status = "done"
            task.finished_at = datetime.now(UTC)

    except BaseException as exc:
        # Catch BaseException so even an asyncio.CancelledError (e.g. process
        # shutdown mid-run) still flips the task to error before we release
        # the lock and re-raise; the operator inspecting the task later sees
        # an explicit reason instead of "stuck running".
        if task.status == "running":
            task.status = "error"
            task.error_reason = f"unexpected: {type(exc).__name__}"
            task.finished_at = datetime.now(UTC)
        logger.exception("backfill: unhandled error for intent_id=%d", intent_id)
        raise
    finally:
        try:
            scheduler.resume_job(job_id)
        except JobLookupError:
            # Intent may have been DELETE'd mid-run (cascade removed the job).
            pass
        except Exception as exc:
            # APScheduler may be mid-shutdown (SchedulerNotRunningError) or
            # the jobstore may have failed under disk-full / OOM.  Log and
            # swallow — finally must NOT raise from cleanup or it would
            # mask whatever the try block was already propagating.
            logger.warning(
                "backfill: resume_job(%s) raised %s during cleanup; ignoring",
                job_id,
                type(exc).__name__,
            )
        try:
            release_intent(intent_id)
        except Exception as exc:
            logger.warning(
                "backfill: release_intent(%d) raised %s during cleanup; ignoring",
                intent_id,
                type(exc).__name__,
            )

# SPDX-License-Identifier: Apache-2.0
"""Weekly KB health check + auto-fix — v2 thread model.

Deterministic, low-risk cleanup only (the auto-fix discipline): de-duplicate
threads (merge same-key blocks + collapse same-day timeline entries), archive
whole stale threads, mark (don't delete) malformed thread headings. Anything
ambiguous is marked, never deleted; git history is the rollback net.

Two triggers share ``run_for_intent``: the weekly APScheduler job
(``add_kb_lint_job``) and the manual ``POST /api/kb/{id}/lint`` button (O2).
Semantic checks needing an LLM (near-synonym thread merge) are deferred.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from sembr.kb import merge as _merge
from sembr.kb.store import _LINT_IDENTITY, KbStore

logger = logging.getLogger(__name__)

_MALFORMED_MARK = "<!--lint:malformed-->"


@dataclass
class LintStats:
    merged_dups: int = 0  # duplicate-key threads merged away
    archived: int = 0  # threads moved to 已归档
    marked: int = 0  # malformed headings marked
    empty_sections: int = 0  # always 0 in v2 — sections are derived from threads
    skipped: str | None = None

    @property
    def changed(self) -> int:
        return self.merged_dups + self.archived + self.marked


def dedup_threads(content: str) -> tuple[str, int]:
    """Merge same-key thread blocks into one (union timelines, keep latest current);
    also collapse same-day timeline entries within a thread. Non-lossy."""
    threads, leading = _merge.parse_doc(content)
    by_key: dict[str, _merge.Thread] = {}
    order: list[str] = []
    removed = 0
    for t in threads:
        # collapse this thread's own same-day entries (last text wins)
        ed: dict[str, str] = {}
        for d, x in t.entries:
            ed[d] = x
        t.entries = sorted(ed.items())
        prev = by_key.get(t.key)
        if prev is None:
            by_key[t.key] = t
            order.append(t.key)
            continue
        removed += 1
        merged: dict[str, str] = dict(prev.entries)
        for d, x in t.entries:
            merged[d] = x
        prev.entries = sorted(merged.items())
        if t.last and (not prev.last or t.last > prev.last):
            prev.last, prev.current = t.last, t.current
        if t.first and (not prev.first or t.first < prev.first):
            prev.first = t.first
    rendered = _merge.render_doc([by_key[k] for k in order], leading)
    return rendered, removed


def archive_stale(content: str, today: str) -> tuple[str, int]:
    threads, leading = _merge.parse_doc(content)
    n = _merge.archive_expired(threads, today)
    return _merge.render_doc(threads, leading), n


def mark_malformed(content: str) -> tuple[str, int]:
    """Mark ``### `` headings that lost their ``<!--k:-->`` anchor. Mark, not delete."""
    lines = content.splitlines()
    n = 0
    for i, line in enumerate(lines):
        s = line.lstrip()
        if s.startswith("### ") and "<!--k:" not in line and _MALFORMED_MARK not in line:
            lines[i] = line.rstrip() + " " + _MALFORMED_MARK
            n += 1
    return ("\n".join(lines) + "\n", n) if n else (content, 0)


def lint_content(content: str, today: str) -> tuple[str, LintStats]:
    """Run all deterministic checks; return cleaned content + stats (pure)."""
    content, merged = dedup_threads(content)
    content, archived = archive_stale(content, today)
    content, marked = mark_malformed(content)
    return content, LintStats(merged_dups=merged, archived=archived, marked=marked)


async def run_for_intent(
    store: KbStore,
    intent_id: int,
    *,
    identity: tuple[str, str] = _LINT_IDENTITY,
    now: datetime | None = None,
    kind: str = "events",
) -> LintStats:
    """Lint one intent's KB and commit if anything changed. Skips (no error) when
    the KB isn't built yet. ``identity`` distinguishes weekly vs manual in git."""
    existing = store.read(intent_id, kind)
    if existing is None:
        return LintStats(skipped="not_bootstrapped")
    now_date = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    cleaned, stats = lint_content(existing, now_date)
    if cleaned == existing:
        return stats
    msg = (
        f"lint intent-{intent_id} {kind}: {stats.merged_dups} merged, "
        f"{stats.archived} archived, {stats.marked} marked"
    )
    await store.write(intent_id, cleaned, kind=kind, identity=identity, message=msg)
    return stats


async def _weekly_lint(store: KbStore) -> None:
    """Weekly job body: lint every kb_enabled intent. Never raises (logs per intent)."""
    from sembr.db.intents import list_intents
    from sembr.db.sqlite import get_conn

    try:
        intents = await list_intents(get_conn(), enabled=None)
    except Exception:
        logger.warning("weekly kb lint: failed to list intents", exc_info=True)
        return
    for intent in intents:
        if not intent.kb_enabled:
            continue
        try:
            stats = await run_for_intent(store, intent.id)
            if stats.changed:
                logger.info("weekly kb lint intent-%d: %s", intent.id, stats)
        except Exception:
            logger.warning("weekly kb lint failed for intent-%d", intent.id, exc_info=True)


def add_kb_lint_job(scheduler: AsyncIOScheduler, store: KbStore) -> None:
    """Register the weekly KB lint job (Mon 04:00). Idempotent via replace_existing.

    APScheduler discipline (memory feedback_apscheduler_next_run_time): no
    next_run_time=None (paused state); replace_existing=True safe for this single job.
    """
    scheduler.add_job(
        _weekly_lint,
        trigger=CronTrigger(day_of_week="mon", hour=4, minute=0),
        id="weekly-kb-lint",
        coalesce=True,
        max_instances=1,
        replace_existing=True,
        args=[store],
    )

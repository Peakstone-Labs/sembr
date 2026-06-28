# SPDX-License-Identifier: Apache-2.0
"""Weekly KB health check + auto-fix (design §7.2).

Lint is **deterministic, low-risk cleanup only** — the auto-fix discipline (design
§7.2): de-duplicate keys, archive stale events, mark (don't delete) malformed
lines, drop empty sections. Anything ambiguous is *marked*, never deleted; git
history is the rollback safety net (we don't run a "suggest & confirm" flow).

Two triggers share this one core (`run_for_intent`): the weekly APScheduler job
(``add_kb_lint_job``) and the manual ``POST /api/kb/{id}/lint`` button (O2).
Semantic checks that need an LLM (near-synonym key merge, misplaced-section
detection) are deferred — see design §13 (F6) / §7.2.
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
    merged_dups: int = 0
    archived: int = 0
    marked: int = 0
    empty_sections: int = 0
    skipped: str | None = None

    @property
    def changed(self) -> int:
        return self.merged_dups + self.archived + self.marked + self.empty_sections


def dedup_keys(content: str) -> tuple[str, int]:
    """Collapse duplicate-key event lines to one, keeping the latest ``最新`` date.

    Duplicate keys are the same event (e.g. a hand-edit re-added a line, or merge
    raced), so keeping the freshest is non-lossy. Lines that don't parse are left
    alone (handled by mark_malformed).
    """
    lines = content.splitlines()
    # key -> index of the line we keep (the one with the max latest-date).
    keep: dict[str, int] = {}
    drop: set[int] = set()
    for i, line in enumerate(lines):
        parsed = _merge._parse_line(line)
        if parsed is None:
            continue
        prev = keep.get(parsed.key)
        if prev is None:
            keep[parsed.key] = i
            continue
        prev_parsed = _merge._parse_line(lines[prev])
        assert prev_parsed is not None
        # Keep the later date; drop the other.
        if parsed.last >= prev_parsed.last:
            drop.add(prev)
            keep[parsed.key] = i
        else:
            drop.add(i)
    if not drop:
        return content, 0
    kept = [ln for i, ln in enumerate(lines) if i not in drop]
    return "\n".join(kept) + "\n", len(drop)


def mark_malformed(content: str) -> tuple[str, int]:
    """Append a malformed marker to broken event lines (have a key anchor but fail
    canonical parse). Mark, never delete — avoids dropping a real event (design §7.2)."""
    lines = content.splitlines()
    n = 0
    for i, line in enumerate(lines):
        if "<!--k:" in line and _merge._parse_line(line) is None and _MALFORMED_MARK not in line:
            lines[i] = line.rstrip() + " " + _MALFORMED_MARK
            n += 1
    if n == 0:
        return content, 0
    return "\n".join(lines) + "\n", n


def remove_empty_sections(content: str) -> tuple[str, int]:
    """Drop ``## section`` headers that have no content lines before the next header."""
    lines = content.splitlines()
    keep = [True] * len(lines)
    section_idx: int | None = None
    has_content = False
    for i, line in enumerate(lines):
        if _merge._SECTION_RE.match(line):
            if section_idx is not None and not has_content:
                keep[section_idx] = False
            section_idx = i
            has_content = False
        elif line.strip():
            has_content = True
    if section_idx is not None and not has_content:
        keep[section_idx] = False
    removed = keep.count(False)
    if removed == 0:
        return content, 0
    return "\n".join(ln for i, ln in enumerate(lines) if keep[i]) + "\n", removed


def lint_content(content: str, now_date: str) -> tuple[str, LintStats]:
    """Run all deterministic checks; return cleaned content + stats (pure)."""
    stats = LintStats()
    content, stats.merged_dups = dedup_keys(content)
    content, stats.archived = _merge.archive_expired(content, now_date)
    content, stats.marked = mark_malformed(content)
    content, stats.empty_sections = remove_empty_sections(content)
    return content, stats


async def run_for_intent(
    store: KbStore,
    intent_id: int,
    *,
    identity: tuple[str, str] = _LINT_IDENTITY,
    now: datetime | None = None,
    kind: str = "events",
) -> LintStats:
    """Lint one intent's KB and commit if anything changed.

    Skips (no error) when the KB isn't built yet. Used by both the weekly job and
    the manual endpoint; ``identity`` distinguishes the two in git history.
    """
    existing = store.read(intent_id, kind)
    if existing is None:
        return LintStats(skipped="not_bootstrapped")
    now_date = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    cleaned, stats = lint_content(existing, now_date)
    if stats.changed == 0 or cleaned == existing:
        return stats
    msg = (
        f"lint intent-{intent_id} {kind}: {stats.merged_dups} merged, "
        f"{stats.archived} archived, {stats.marked} marked, {stats.empty_sections} empty-sections"
    )
    await store.write(intent_id, cleaned, kind=kind, identity=identity, message=msg)
    return stats


async def _weekly_lint(store: KbStore) -> None:
    """Weekly job body: lint every kb_enabled intent. Never raises (logs per intent)."""
    # Imported here to avoid a circular import at module load (db.intents → models).
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

    Follows the APScheduler discipline (memory feedback_apscheduler_next_run_time):
    no next_run_time=None (that is the paused state); replace_existing=True is safe
    for this single, stateless job.
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

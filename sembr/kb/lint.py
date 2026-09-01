# SPDX-License-Identifier: Apache-2.0
"""Weekly KB health check + auto-fix — v2 thread model.

Two layers:
- **Deterministic** (`lint_content`): merge same-key thread blocks + collapse
  same-day timeline entries, archive whole stale threads, mark (don't delete)
  malformed headings.
- **LLM near-duplicate merge** (`merge_near_duplicates`, R2a): the LLM only
  *picks* which threads are the same topic; code deterministically unions their
  timelines (non-lossy, git-revertible). Runs only when a backend is supplied.

Two triggers share ``run_for_intent``: the weekly APScheduler job
(``add_kb_lint_job``) and the manual ``POST /api/kb/{id}/lint`` button (O2).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, field_validator

from sembr.kb import merge as _merge
from sembr.kb.store import _LINT_IDENTITY, KbStore
from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_MALFORMED_MARK = "<!--lint:malformed-->"


@dataclass
class LintStats:
    merged_dups: int = 0  # exact duplicate-key thread blocks merged away
    merged_near_dup: int = 0  # near-duplicate (semantic) threads merged away (LLM)
    archived: int = 0  # threads moved to 已归档
    marked: int = 0  # malformed headings marked
    empty_sections: int = 0  # always 0 in v2 — sections are derived from threads
    skipped: str | None = None

    @property
    def changed(self) -> int:
        return self.merged_dups + self.merged_near_dup + self.archived + self.marked


# --------------------------------------------------------------------------- #
# Deterministic checks
# --------------------------------------------------------------------------- #


def dedup_threads(content: str) -> tuple[str, int]:
    """Merge same-key thread blocks into one (union timelines, keep latest current);
    also collapse same-day timeline entries within a thread. Non-lossy."""
    threads, leading = _merge.parse_doc(content)
    by_key: dict[str, _merge.Thread] = {}
    order: list[str] = []
    removed = 0
    for t in threads:
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


# --------------------------------------------------------------------------- #
# LLM near-duplicate thread merge (R2a) — LLM picks groups, code merges
# --------------------------------------------------------------------------- #

_NEARDUP_SYSTEM = """你在维护一个长期事件追踪索引。给你当前所有追踪线索(每条 key | 标题 | 当前状态)。找出【其实是同一主题、应该合并】的线索组。

- 只合并明显是同一主题/同一事件演进的线索;**有任何不确定就不要合并**(宁缺毋滥)。
- 每组输出:`canonical_key`(保留的 key,**必须从该组已有的 key 里选一个**)、`canonical_title`(合并后的标题)、`section`(合并后分节)、`merge_keys`(该组要合并的所有 key,含 canonical_key,**至少 2 个**)。
- 没有需要合并的就输出空数组。

只输出 JSON {"groups":[{"canonical_key":"...","canonical_title":"...","section":"...","merge_keys":["k1","k2"]}]}。"""


class _MergeGroup(BaseModel):
    canonical_key: str
    canonical_title: str
    section: str
    merge_keys: list[str]

    @field_validator("canonical_key")
    @classmethod
    def _slug(cls, v: str) -> str:
        return _merge.slugify(v)


class _NearDupResult(BaseModel):
    groups: list[_MergeGroup]


def _apply_merge_group(by_key: dict[str, _merge.Thread], g: _MergeGroup, dropped: set[str]) -> int:
    """Fold a group's threads into one canonical thread (union timelines). Returns
    how many threads were merged away (0 if the group is invalid)."""
    keys = [k for k in dict.fromkeys(g.merge_keys) if k in by_key and k not in dropped]
    if len(keys) < 2:
        return 0
    canon_key = g.canonical_key if g.canonical_key in keys else keys[0]
    canon = by_key[canon_key]
    entries: dict[str, str] = {}
    first, last, current = canon.first, canon.last, canon.current
    for k in keys:
        t = by_key[k]
        for d, x in t.entries:
            entries[d] = x  # same-day collision: last wins (same topic — acceptable)
        if t.first and (not first or t.first < first):
            first = t.first
        if t.last and (not last or t.last > last):
            last, current = t.last, t.current
    canon.entries = sorted(entries.items())
    canon.first, canon.last, canon.current = first, last, current
    canon.title = g.canonical_title.strip() or canon.title
    canon.section = g.section.strip() or canon.section
    for k in keys:
        if k != canon_key:
            dropped.add(k)
    return len(keys) - 1


async def merge_near_duplicates(
    content: str, backend: BaseLLMBackend, model: str | None
) -> tuple[str, int]:
    """LLM identifies same-topic thread groups; code unions their timelines.

    Non-lossy (timelines merged, not dropped) and git-revertible. Conservative by
    prompt (宁缺毋滥). No-op when <2 threads or the LLM returns no groups."""
    threads, leading = _merge.parse_doc(content)
    if len(threads) < 2:
        return content, 0
    by_key = {t.key: t for t in threads}
    listing = "\n".join(f"- {t.key} | {t.title} | 当前：{t.current}" for t in threads)
    prompt = f"## 当前所有追踪线索({len(threads)} 条)\n{listing}\n\n找出应合并的线索组(JSON)。"
    result = await backend.structured(prompt, _NearDupResult, system=_NEARDUP_SYSTEM, model=model)
    dropped: set[str] = set()
    n_merged = 0
    for g in result.groups:
        n_merged += _apply_merge_group(by_key, g, dropped)
    if not dropped:
        return content, 0
    survivors = [t for t in threads if t.key not in dropped]
    return _merge.render_doc(survivors, leading), n_merged


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


async def run_for_intent(
    store: KbStore,
    intent_id: int,
    *,
    identity: tuple[str, str] = _LINT_IDENTITY,
    now: datetime | None = None,
    kind: str = "events",
    backend: BaseLLMBackend | None = None,
    model: str | None = None,
) -> LintStats:
    """Lint one intent's KB and commit if anything changed. Skips (no error) when
    the KB isn't built yet. With a ``backend``, also runs the LLM near-duplicate
    merge first. ``identity`` distinguishes weekly vs manual in git."""
    existing = store.read(intent_id, kind)
    if existing is None:
        return LintStats(skipped="not_bootstrapped")
    content = existing
    n_near = 0
    if backend is not None:
        try:
            content, n_near = await merge_near_duplicates(content, backend, model)
        except Exception:
            logger.warning("kb lint: near-dup merge failed for intent-%d", intent_id, exc_info=True)
    now_date = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    cleaned, stats = lint_content(content, now_date)
    stats.merged_near_dup = n_near
    if cleaned == existing:
        return stats
    msg = (
        f"lint intent-{intent_id} {kind}: {stats.merged_dups} merged, "
        f"{stats.merged_near_dup} near-dup, {stats.archived} archived, {stats.marked} marked"
    )
    await store.write(intent_id, cleaned, kind=kind, identity=identity, message=msg)
    return stats


async def _weekly_lint(store: KbStore, backend: BaseLLMBackend | None, model: str | None) -> None:
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
            stats = await run_for_intent(store, intent.id, backend=backend, model=model)
            if stats.changed:
                logger.info("weekly kb lint intent-%d: %s", intent.id, stats)
        except Exception:
            logger.warning("weekly kb lint failed for intent-%d", intent.id, exc_info=True)


def add_kb_lint_job(
    scheduler: AsyncIOScheduler,
    store: KbStore,
    backend: BaseLLMBackend | None = None,
    model: str | None = None,
) -> None:
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
        args=[store, backend, model],
    )

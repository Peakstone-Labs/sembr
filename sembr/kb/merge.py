# SPDX-License-Identifier: Apache-2.0
"""Incremental event-index merge (design §4, recommended scheme B).

The hard constraints — *converge, don't drop, git-auditable* — are guaranteed by
**code**, not by the LLM. The LLM only does the narrow, low-risk part it's good
at: assign each new digest bullet a stable event key (reusing an existing key on
a clear same-event match, else a new slug) and a one-line latest state. All
mutation of the file (which line changes, what's appended, what's archived) is
deterministic here, so we never let the model rewrite the whole page and silently
lose events (the Phase-3 wash lesson, design §4.1).

`events.md` is the single human-readable source of truth. Each event is one line:

    - <!--k:7day-reverse-repo--> **7天逆回购利率**（首见 2026-05-12，最新 2026-06-27）：下调10bp至1.40%

The ``<!--k:slug-->`` HTML comment is the deterministic key anchor: invisible when
rendered, visible in git diff, regex-extractable by this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, field_validator

from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

# Tunables (design §4.3; on-box calibrated). RETENTION_DAYS = user decision O1.
RETENTION_DAYS = 30
CHUNK_MIN = 3  # fewer candidates than this ⇒ digest format likely changed → skip (F5)
ARCHIVE_SECTION = "已归档"
FALLBACK_SECTION = "未分类"  # used when the LLM proposes a section not already present (F6)

_DATE_FMT = "%Y-%m-%d"
_RUN_AT_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Canonical event line. We own this format (code builds every line on create and
# rebuilds it on update), so a strict regex is safe; non-matching lines are left
# untouched here and handled by lint (malformed marking, design §7.2).
_LINE_RE = re.compile(
    r"^\s*[-*]\s+<!--k:(?P<key>[a-z0-9][a-z0-9-]*)-->\s*"
    r"\*\*(?P<title>.+?)\*\*"
    r"（首见\s*(?P<first>\d{4}-\d{2}-\d{2})，最新\s*(?P<last>\d{4}-\d{2}-\d{2})）："
    r"(?P<state>.*\S)\s*$"
)
_KEY_RE = re.compile(r"<!--k:([a-z0-9][a-z0-9-]*)-->")
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<body>.+\S)\s*$")
# Strip a leading delta label like [新增] / 【升级】 from a digest bullet.
_LABEL_RE = re.compile(r"^\s*[\[【][^\]】]{1,8}[\]】]\s*")
_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    """Deterministically canonicalize a key to ``[a-z0-9-]`` (defends F6/F10).

    The LLM is asked for a slug but may return spaces/case/unicode; we never trust
    it to be well-formed. Non-ascii (e.g. Chinese) collapses to empty, so we fall
    back to a stable hash-free placeholder built from the title at the call site.
    """
    s = _SLUG_BAD.sub("-", raw.strip().lower()).strip("-")
    return s


@dataclass
class Candidate:
    """One deterministic chunk of today's digest (design §4.1 step 1)."""

    text: str
    section: str | None


@dataclass
class MergeStats:
    new: int = 0
    updated: int = 0
    archived: int = 0
    skipped: str | None = None  # None = applied; else reason ("low_candidates")


@dataclass
class MergeResult:
    content: str
    stats: MergeStats = field(default_factory=MergeStats)


class _Assignment(BaseModel):
    candidate_index: int
    key: str
    is_new: bool
    title: str
    section: str
    state: str

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        return slugify(v)


class _AssignResult(BaseModel):
    assignments: list[_Assignment]


# --------------------------------------------------------------------------- #
# Deterministic helpers (no LLM — unit-testable)
# --------------------------------------------------------------------------- #


def chunk_digest(digest_text: str) -> list[Candidate]:
    """Split a digest into candidate events: one per bullet, tagged with its section.

    Deterministic, depends on the digest being bullet/section structured (the
    standard summary format). Leading delta labels are stripped so the candidate
    text is the bare event content. If this yields < CHUNK_MIN the caller skips
    the day (digest format likely changed — F5).
    """
    candidates: list[Candidate] = []
    current_section: str | None = None
    for line in digest_text.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            current_section = sec.group("title")
            continue
        bullet = _BULLET_RE.match(line)
        if not bullet:
            continue
        body = _LABEL_RE.sub("", bullet.group("body")).strip()
        if body:
            candidates.append(Candidate(text=body, section=current_section))
    return candidates


@dataclass
class _ParsedLine:
    key: str
    title: str
    first: str
    last: str
    state: str


def _parse_line(line: str) -> _ParsedLine | None:
    m = _LINE_RE.match(line)
    if not m:
        return None
    return _ParsedLine(m["key"], m["title"], m["first"], m["last"], m["state"])


def build_line(key: str, title: str, first: str, last: str, state: str) -> str:
    return f"- <!--k:{key}--> **{title}**（首见 {first}，最新 {last}）：{state}"


def parse_events(events_md: str) -> dict[str, _ParsedLine]:
    """Map event key → parsed line for every canonical event line in the doc."""
    out: dict[str, _ParsedLine] = {}
    for line in events_md.splitlines():
        parsed = _parse_line(line)
        if parsed:
            out[parsed.key] = parsed
    return out


def existing_keys(events_md: str) -> set[str]:
    return set(_KEY_RE.findall(events_md))


def section_titles(events_md: str) -> list[str]:
    return [m.group("title") for line in events_md.splitlines() if (m := _SECTION_RE.match(line))]


def _find_key_line(lines: list[str], key: str) -> int | None:
    """Current index of the canonical line for *key*, or None.

    Resolved fresh on each use (never cached) — inserting a new line shifts all
    later indices, so a cached map would go stale mid-merge.
    """
    anchor = f"<!--k:{key}-->"
    for i, line in enumerate(lines):
        if anchor in line and _parse_line(line) is not None:
            return i
    return None


def _insert_under_section(lines: list[str], section: str, new_line: str) -> None:
    """Insert *new_line* right after the ``## section`` header (creating it if absent)."""
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if m and m.group("title") == section:
            lines.insert(i + 1, new_line)
            return
    # Section missing → create it at end of doc, then the line under it.
    if lines and lines[-1].strip():
        lines.append("")
    lines.append(f"## {section}")
    lines.append(new_line)


def apply_merge(
    events_md: str,
    candidates: list[Candidate],
    assignments: list[_Assignment],
    now_date: str,
) -> tuple[str, MergeStats]:
    """Apply assignments deterministically. Code — not the LLM — guarantees no drop.

    - existing key → rebuild ONLY that line: keep title + first-seen, set latest =
      now, replace state. Every other line is byte-preserved.
    - new key → build a fresh line and append under its (validated) section.

    A new key is only treated as "existing update" when the key is actually
    present; otherwise it's appended — so a model that wrongly flags ``is_new`` for
    an event with a matching key still can't clobber an unrelated line (F10).
    """
    lines = events_md.splitlines()
    present_keys = set(parse_events(events_md))
    present_sections = set(section_titles(events_md))
    stats = MergeStats()

    for a in assignments:
        if not (0 <= a.candidate_index < len(candidates)):
            continue  # model referenced a candidate that doesn't exist — ignore
        key = a.key or slugify(a.title) or f"event-{a.candidate_index}"
        # Index is resolved fresh (not cached): a prior new-line insertion shifts
        # all later indices, so a cached map would point at the wrong line.
        idx = _find_key_line(lines, key) if (not a.is_new and key in present_keys) else None
        if idx is not None:
            parsed = _parse_line(lines[idx])
            assert parsed is not None  # _find_key_line only returns parseable lines
            lines[idx] = build_line(key, parsed.title, parsed.first, now_date, a.state)
            stats.updated += 1
        else:
            # New (or claimed-existing but key absent): append, never overwrite.
            section = a.section if a.section in present_sections else FALLBACK_SECTION
            new_line = build_line(key, a.title, now_date, now_date, a.state)
            _insert_under_section(lines, section, new_line)
            present_sections.add(section)
            present_keys.add(key)
            stats.new += 1

    return "\n".join(lines) + "\n", stats


def archive_expired(
    events_md: str,
    now_date: str,
    retention_days: int = RETENTION_DAYS,
) -> tuple[str, int]:
    """Move events whose latest date is older than retention into ``## 已归档``.

    Archive, don't delete (design O1/C1 — never lose history). Lines already under
    the archive section are left in place.
    """
    cutoff = datetime.strptime(now_date, _DATE_FMT).replace(tzinfo=UTC) - timedelta(
        days=retention_days
    )
    lines = events_md.splitlines()
    kept: list[str] = []
    moved: list[str] = []
    current_section: str | None = None
    for line in lines:
        sec = _SECTION_RE.match(line)
        if sec:
            current_section = sec.group("title")
            kept.append(line)
            continue
        parsed = _parse_line(line)
        if parsed and current_section != ARCHIVE_SECTION:
            try:
                last = datetime.strptime(parsed.last, _DATE_FMT).replace(tzinfo=UTC)
            except ValueError:
                kept.append(line)  # unparseable date — leave for lint, don't drop
                continue
            if last < cutoff:
                moved.append(line)
                continue
        kept.append(line)

    if not moved:
        return events_md, 0

    if ARCHIVE_SECTION not in section_titles("\n".join(kept)):
        if kept and kept[-1].strip():
            kept.append("")
        kept.append(f"## {ARCHIVE_SECTION}")
    # Append moved lines after the archive header (at end of doc).
    for line in moved:
        _insert_under_section(kept, ARCHIVE_SECTION, line)
    return "\n".join(kept) + "\n", len(moved)


# --------------------------------------------------------------------------- #
# LLM key-assignment (the only non-deterministic step)
# --------------------------------------------------------------------------- #

_ASSIGN_SYSTEM = """你在维护一个长期事件进展索引。给你今天简报里抽出的若干"候选事件"、当前索引里**已存在的事件键清单**、以及当前索引的**章节标题清单**。

为每个候选事件输出一条 assignment：
- `key`：事件键(小写英文/拼音连字符 slug)。**只有当候选与某个已存在键确属同一事件时才复用该键**;有任何不确定就造一个新 slug(`is_new=true`)。宁可新建也不要把语义不同的事件强行并入已有键。
- `is_new`：复用已存在键填 false;新事件填 true。
- `title`：简短事件名(不带 markdown)。
- `section`：从给定章节标题里选最贴切的一个;若都不合适,原样给出你认为合适的标题(系统会回退到"未分类")。
- `state`：该事件**最新状态**的一句话(含关键数字),用于写进索引行。

只输出 JSON,形如 {"assignments":[{"candidate_index":0,"key":"...","is_new":false,"title":"...","section":"...","state":"..."}]}。每个候选必须恰好一条,candidate_index 从 0 开始。"""


def _build_assign_prompt(candidates: list[Candidate], keys: set[str], sections: list[str]) -> str:
    cand_block = "\n".join(
        f"[{i}] (原章节: {c.section or '无'}) {c.text}" for i, c in enumerate(candidates)
    )
    keys_block = ", ".join(sorted(keys)) if keys else "(空,目前没有已存在键)"
    sec_block = ", ".join(sections) if sections else "(空)"
    return (
        f"## 已存在的事件键清单\n{keys_block}\n\n"
        f"## 当前索引章节标题清单\n{sec_block}\n\n"
        f"## 今天的候选事件({len(candidates)} 条)\n{cand_block}\n\n"
        "为每个候选输出 assignment(JSON)。"
    )


async def assign_keys(
    candidates: list[Candidate],
    keys: set[str],
    sections: list[str],
    backend: BaseLLMBackend,
    model: str | None,
) -> list[_Assignment]:
    """LLM step: assign each candidate a (possibly reused) key + latest state."""
    prompt = _build_assign_prompt(candidates, keys, sections)
    result = await backend.structured(prompt, _AssignResult, system=_ASSIGN_SYSTEM, model=model)
    return result.assignments


async def merge_digest(
    events_md: str,
    digest_text: str,
    run_at: str,
    backend: BaseLLMBackend,
    model: str | None,
) -> MergeResult:
    """Incrementally merge today's digest into an existing events.md.

    Caller (store) guarantees ``events_md`` is the current content (bootstrap done).
    Returns the new content + stats; a skip (low candidate count) returns the
    input unchanged with ``stats.skipped`` set.
    """
    now_date = run_at[:10] if len(run_at) >= 10 else datetime.now(UTC).strftime(_DATE_FMT)
    candidates = chunk_digest(digest_text)
    if len(candidates) < CHUNK_MIN:
        logger.warning(
            "kb merge: only %d candidate(s) (< CHUNK_MIN=%d) — digest format may have "
            "changed; skipping ingest for run_at=%s",
            len(candidates),
            CHUNK_MIN,
            run_at,
        )
        return MergeResult(content=events_md, stats=MergeStats(skipped="low_candidates"))

    assignments = await assign_keys(
        candidates, existing_keys(events_md), section_titles(events_md), backend, model
    )
    merged, stats = apply_merge(events_md, candidates, assignments, now_date)
    merged, n_archived = archive_expired(merged, now_date)
    stats.archived = n_archived
    return MergeResult(content=merged, stats=stats)

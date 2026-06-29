# SPDX-License-Identifier: Apache-2.0
"""Incremental event-index merge — v2 "tracked thread + timeline" model.

Each event is a coarse **tracked thread** (target ~10-20 per intent), rendered as
a markdown block:

    ## 海峡通行与管控

    ### 海峡开闭与管控权 <!--k:strait-control-->
    首见 2026-06-13 · 最新 2026-06-29 · 当前：伊朗坚持管理权，通行受阻但部分恢复
    - 2026-06-17 美伊签 MOU，规定立即开放(60天免费)、解封、启动核谈
    - 2026-06-21 伊朗正式宣布关闭海峡；美军否认
    - 2026-06-29 伊朗外长重申管理权，革命卫队要求协调；IMO 暂停撤离

A daily merge consolidates that day's digest points into **at most one dated entry
per thread** (appended to the timeline), matching candidates against existing
threads by *title + current state* (not bare slugs). The hard guarantees —
converge / don't drop / git-auditable — stay in deterministic code; the LLM only
decides "which thread does today's news belong to + one-line progress + updated
current state". Append-only timelines are naturally non-lossy and show how an
event evolved, not just its latest snapshot.

This supersedes the v1 one-line-per-event (latest-state-only) model.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, field_validator

from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

# Tunables (events-v2 decisions; on-box calibrated).
RETENTION_DAYS = 30  # thread with no update for this many days → archived (whole block)
CHUNK_MIN = 3  # fewer candidates ⇒ digest format likely changed → skip (F5)
ARCHIVE_SECTION = "已归档"
FALLBACK_SECTION = "未分类"

_DATE_FMT = "%Y-%m-%d"

# --- structure regexes (we own this format; strict match, tolerant on misses) ---
_SECTION_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$")
_THREAD_RE = re.compile(r"^###\s+(?P<title>.+?)\s*<!--k:(?P<key>[a-z0-9][a-z0-9-]*)-->\s*$")
_META_RE = re.compile(
    r"^首见\s*(?P<first>\d{4}-\d{2}-\d{2})\s*·\s*最新\s*(?P<last>\d{4}-\d{2}-\d{2})"
    r"\s*·\s*当前[：:]\s*(?P<current>.*\S)\s*$"
)
_ENTRY_RE = re.compile(r"^-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+(?P<text>.*\S)\s*$")
_KEY_RE = re.compile(r"<!--k:([a-z0-9][a-z0-9-]*)-->")
# digest chunking
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<body>.*\S)\s*$")
_LABEL_RE = re.compile(r"^\s*[\[【][^\]】]{1,8}[\]】]\s*")
_SLUG_BAD = re.compile(r"[^a-z0-9]+")


def slugify(raw: str) -> str:
    """Canonicalize to ``[a-z0-9-]``; non-ascii (Chinese) collapses to '' → caller
    falls back to ``stable_fallback_key`` (content hash, never positional)."""
    return _SLUG_BAD.sub("-", raw.strip().lower()).strip("-")


def stable_fallback_key(text: str) -> str:
    """Content-derived fallback key when slugify yields empty (review 🔴-1).

    Never positional: same text → same key across days; distinct text → distinct.
    """
    return "event-" + hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:10]


@dataclass
class Candidate:
    text: str
    section: str | None


@dataclass
class Thread:
    """One tracked event: a coarse topic with a dated progress timeline."""

    key: str
    title: str
    section: str
    first: str
    last: str
    current: str
    entries: list[tuple[str, str]] = field(default_factory=list)  # (ISO date, text)
    extra: list[str] = field(default_factory=list)  # verbatim hand-edited stray lines


@dataclass
class MergeStats:
    new: int = 0  # new threads created
    updated: int = 0  # existing threads that got a fresh timeline entry
    archived: int = 0  # threads moved to 已归档
    skipped: str | None = None


@dataclass
class MergeResult:
    content: str
    stats: MergeStats = field(default_factory=MergeStats)


class _ThreadUpdate(BaseModel):
    key: str
    is_new: bool
    title: str
    section: str
    entry: str  # one-line progress for TODAY on this thread
    current_state: str  # updated one-line current status

    @field_validator("key")
    @classmethod
    def _slug(cls, v: str) -> str:
        return slugify(v)


class _AssignResult(BaseModel):
    updates: list[_ThreadUpdate]


# --------------------------------------------------------------------------- #
# Deterministic parse / render (no LLM — unit-testable)
# --------------------------------------------------------------------------- #


def chunk_digest(digest_text: str) -> list[Candidate]:
    """Split a digest into candidate points (one per bullet, tagged with section)."""
    out: list[Candidate] = []
    section: str | None = None
    for line in digest_text.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            section = sec.group("title")
            continue
        b = _BULLET_RE.match(line)
        if not b:
            continue
        body = _LABEL_RE.sub("", b.group("body")).strip()
        if body:
            out.append(Candidate(text=body, section=section))
    return out


def parse_doc(events_md: str) -> tuple[list[Thread], list[str]]:
    """Parse events.md into ordered threads + any leading orphan lines.

    Tolerant: a ``### head`` starts a thread; the first ``首见…`` line fills its
    meta; ``- DATE text`` lines are timeline entries; any other non-blank line
    inside a block is preserved verbatim in ``thread.extra`` (so hand-edits aren't
    dropped — C1). Lines before the first thread are kept as leading orphans.
    """
    threads: list[Thread] = []
    leading: list[str] = []
    section: str | None = None
    cur: Thread | None = None
    for line in events_md.splitlines():
        sec = _SECTION_RE.match(line)
        if sec:
            section = sec.group("title")
            cur = None
            continue
        head = _THREAD_RE.match(line)
        if head:
            cur = Thread(
                key=head.group("key"),
                title=head.group("title").strip().strip("*").strip(),
                section=section or FALLBACK_SECTION,
                first="",
                last="",
                current="",
            )
            threads.append(cur)
            continue
        if cur is None:
            if line.strip():
                leading.append(line)
            continue
        meta = _META_RE.match(line)
        if meta and not cur.first:
            cur.first, cur.last, cur.current = meta.group("first", "last", "current")
            continue
        ent = _ENTRY_RE.match(line)
        if ent:
            cur.entries.append((ent.group("date"), ent.group("text")))
            continue
        if line.strip():
            cur.extra.append(line)
    # backfill missing first/last from entries (malformed / meta-less blocks)
    for t in threads:
        if t.entries:
            dates = [d for d, _ in t.entries]
            t.first = t.first or min(dates)
            t.last = t.last or max(dates)
    return threads, leading


def render_doc(threads: list[Thread], leading: list[str] | None = None) -> str:
    """Render threads back to markdown, grouped by section (first-appearance order;
    archive section last). Timeline entries sorted by date. Deterministic."""
    out: list[str] = []
    if leading:
        out.extend(leading)
        out.append("")
    order: list[str] = []
    by_sec: dict[str, list[Thread]] = {}
    for t in threads:
        s = t.section or FALLBACK_SECTION
        if s not in by_sec:
            by_sec[s] = []
            order.append(s)
        by_sec[s].append(t)
    if ARCHIVE_SECTION in order:
        order = [s for s in order if s != ARCHIVE_SECTION] + [ARCHIVE_SECTION]
    for s in order:
        out.append(f"## {s}")
        out.append("")
        for t in by_sec[s]:
            out.append(f"### {t.title} <!--k:{t.key}-->")
            out.append(f"首见 {t.first} · 最新 {t.last} · 当前：{t.current}")
            for d, text in sorted(t.entries):
                out.append(f"- {d} {text}")
            out.extend(t.extra)
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def parse_events(events_md: str) -> dict[str, Thread]:
    """Map key → Thread (len() = thread count). Kept name for API/probe compat."""
    return {t.key: t for t in parse_doc(events_md)[0]}


def existing_keys(events_md: str) -> set[str]:
    return set(_KEY_RE.findall(events_md))


def section_titles(events_md: str) -> list[str]:
    seen: list[str] = []
    for t in parse_doc(events_md)[0]:
        if t.section not in seen:
            seen.append(t.section)
    return seen


def apply_updates(
    threads: list[Thread], updates: list[_ThreadUpdate], today: str
) -> tuple[list[Thread], MergeStats]:
    """Apply per-thread updates: append one dated entry to matched threads (replacing
    any existing same-day entry — max one/day), create new threads otherwise.

    An update whose key already exists is treated as an UPDATE even if is_new=True,
    so the LLM can't spawn duplicate-key blocks (the v1 day-1 explosion bug)."""
    by_key = {t.key: t for t in threads}
    present_sections = {t.section for t in threads}
    stats = MergeStats()
    for u in updates:
        key = u.key or stable_fallback_key(u.title)
        t = by_key.get(key)
        if t is not None:
            t.entries = [(d, x) for d, x in t.entries if d != today]  # one entry per day
            t.entries.append((today, u.entry))
            t.last = max(t.last, today) if t.last else today
            t.current = u.current_state
            stats.updated += 1
        else:
            section = u.section.strip() if u.section.strip() else FALLBACK_SECTION
            t = Thread(
                key=key,
                title=u.title,
                section=section,
                first=today,
                last=today,
                current=u.current_state,
                entries=[(today, u.entry)],
            )
            threads.append(t)
            by_key[key] = t
            present_sections.add(section)
            stats.new += 1
    return threads, stats


def archive_expired(threads: list[Thread], today: str, retention_days: int = RETENTION_DAYS) -> int:
    """Move whole threads with no update for >retention_days into ``已归档`` (not
    deleted — C1). Returns how many were archived."""
    cutoff = datetime.strptime(today, _DATE_FMT).replace(tzinfo=UTC) - timedelta(
        days=retention_days
    )
    n = 0
    for t in threads:
        if t.section == ARCHIVE_SECTION or not t.last:
            continue
        try:
            last = datetime.strptime(t.last, _DATE_FMT).replace(tzinfo=UTC)
        except ValueError:
            continue
        if last < cutoff:
            t.section = ARCHIVE_SECTION
            n += 1
    return n


# --------------------------------------------------------------------------- #
# LLM thread assignment (the only non-deterministic step)
# --------------------------------------------------------------------------- #

_ASSIGN_SYSTEM = """你在维护一个长期事件【追踪索引】:每个主题是一条"追踪线索",有标题、当前状态、和一条按日期推进的时间线。

给你:今天简报抽出的候选要点、以及当前索引里【已存在的追踪线索】(每条给 key | 标题 | 当前状态)。

把今天的候选要点【归并】到它们所属的线索,为每条【今天有进展】的线索输出一条更新:
- `key`:线索键,**纯 ASCII 小写连字符 slug([a-z0-9-])**,严禁中文。**优先复用已存在线索的 key**——根据标题/当前状态判断该候选属于哪条已有线索;只有确实是全新主题才造新 key 并 `is_new=true`。
- `is_new`:复用已存在线索填 false;全新线索填 true。
- `title`:线索标题(简短,不带 markdown)。
- `section`:主题分节(如 海峡通行与管控/军事冲突/石油市场/外交与国际反应/停火与和平协议 等)。
- `entry`:该线索【今天】的进展,**一句话汇总**(含关键数字)。把同一线索的多个候选要点合并成这一句,**不要为每个要点单独输出**。
- `current_state`:综合后该线索的最新当前状态,一句话。

**粒度要粗**:整个索引维持约 10-20 条线索;绝不要把同一主题拆成很多条。每条线索今天最多一条更新。没有新进展的线索不要输出。

只输出 JSON,形如 {"updates":[{"key":"...","is_new":false,"title":"...","section":"...","entry":"...","current_state":"..."}]}。"""


def _build_assign_prompt(candidates: list[Candidate], threads: list[Thread]) -> str:
    existing = (
        "\n".join(f"- {t.key} | {t.title} | 当前：{t.current}" for t in threads)
        if threads
        else "(空,目前没有已存在线索)"
    )
    cand = "\n".join(f"[{i}] ({c.section or '无'}) {c.text}" for i, c in enumerate(candidates))
    return (
        f"## 已存在的追踪线索({len(threads)} 条)\n{existing}\n\n"
        f"## 今天的候选要点({len(candidates)} 条)\n{cand}\n\n"
        "把候选要点归并到线索,只为今天有进展的线索各输出一条更新(JSON)。"
    )


async def assign_threads(
    candidates: list[Candidate], threads: list[Thread], backend: BaseLLMBackend, model: str | None
) -> list[_ThreadUpdate]:
    prompt = _build_assign_prompt(candidates, threads)
    result = await backend.structured(prompt, _AssignResult, system=_ASSIGN_SYSTEM, model=model)
    return result.updates


async def merge_digest(
    events_md: str,
    digest_text: str,
    run_at: str,
    backend: BaseLLMBackend,
    model: str | None,
) -> MergeResult:
    """Incrementally merge today's digest into an existing events.md (thread model)."""
    now_date = run_at[:10] if len(run_at) >= 10 else datetime.now(UTC).strftime(_DATE_FMT)
    candidates = chunk_digest(digest_text)
    if len(candidates) < CHUNK_MIN:
        logger.warning(
            "kb merge: only %d candidate(s) (< CHUNK_MIN=%d) — skipping ingest for run_at=%s",
            len(candidates),
            CHUNK_MIN,
            run_at,
        )
        return MergeResult(content=events_md, stats=MergeStats(skipped="low_candidates"))

    threads, leading = parse_doc(events_md)
    updates = await assign_threads(candidates, threads, backend, model)
    threads, stats = apply_updates(threads, updates, now_date)
    stats.archived = archive_expired(threads, now_date)
    return MergeResult(content=render_doc(threads, leading), stats=stats)

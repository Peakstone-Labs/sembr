# SPDX-License-Identifier: Apache-2.0
"""KB cold-start distillation — the explicit "rebuild KB" path (O3), v2 thread model.

Building the initial events.md is a one-off (per intent, on demand), so it can
afford a stronger model than the daily merge. We do NOT auto-run this when
``kb_enabled`` is toggled on (F1/O3) — only the explicit rebuild action calls it.

v2: the LLM returns a small set of coarse **tracked threads** (target ~10-20),
each with a distilled **timeline** (dated progress entries) + a current-state
line. We render that deterministically to the canonical block format (reusing
merge.render_doc), so the model never hand-writes anchors/dates and the output is
immediately ingest-mergeable.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from pydantic import BaseModel

from sembr.kb import merge as _merge
from sembr.kb.store import _INGEST_IDENTITY, KbStore
from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_DISTILL_SYSTEM = """你在为一个长期跟踪的主题建立【事件追踪索引】的初始版本。给你过去 N 天的简报历史(每条带日期)。把它归纳成一组【粗粒度的追踪线索】。

要求:
- **粒度要粗**:整个索引约 10-20 条线索。把同一主题/政策工具/冲突方向跨天的进展归到**一条**线索,不要拆碎。
- **线索之间必须互斥(MECE)**:不要出现两条覆盖面重叠的线索(如"管控收费体系"与"通行政策演变")。两个主题只要明显重叠,就**并成一条**;实在要分,边界必须清晰不重叠。
- 每条线索给:`key`(**纯 ASCII 小写连字符 slug,[a-z0-9-]**,如 strait-control、oil-price;严禁中文)、`title`(简短线索名,不带 markdown)、`section`(主题分节,如 海峡通行与管控/军事冲突/石油市场/外交与国际反应/停火与和平协议 等)、`first_seen`(首见 ISO 日期 YYYY-MM-DD)、`current_state`(最新当前状态一句话,含关键数字)、`timeline`(按日期推进的进展条目数组,每条 {date: YYYY-MM-DD, entry: 一句话进展})。
- timeline 体现该线索【怎么一步步演进】,不是只给最新;每个日期最多一条(把当天该线索的要点合并成一句)。

只输出 JSON,形如 {"threads":[{"key":"strait-control","title":"...","section":"...","first_seen":"2026-06-01","current_state":"...","timeline":[{"date":"2026-06-01","entry":"..."}]}]}。不要任何解释。"""

_DISTILL_USER = """## 过去 N 天简报历史(每条带日期)

{history}

---

输出该主题的结构化追踪线索(JSON)。"""


class _TimelineEntry(BaseModel):
    date: str
    entry: str


class _DistillThread(BaseModel):
    key: str = ""  # ASCII slug from the LLM; empty → derived from title (R1b)
    title: str
    section: str
    first_seen: str
    current_state: str
    timeline: list[_TimelineEntry] = []


class _DistillResult(BaseModel):
    threads: list[_DistillThread]


def _safe_date(raw: str, fallback: str) -> str:
    return raw if _ISO_RE.match(raw.strip()) else fallback


def render_threads(threads: list[_DistillThread], now_date: str) -> str:
    """Render distilled threads to canonical events.md (reuses merge.render_doc).

    Keys derive deterministically from the title (slug, else content hash),
    de-duplicated with a numeric suffix — stable anchors the daily merge matches.
    """
    used: set[str] = set()
    built: list[_merge.Thread] = []
    for t in threads:
        # Prefer the LLM's ASCII slug (R1b: readable keys); fall back to a slug of
        # the title, then a content hash (Chinese titles slug to "").
        base = (
            _merge.slugify(t.key) or _merge.slugify(t.title) or _merge.stable_fallback_key(t.title)
        )
        key = base
        n = 2
        while key in used:
            key = f"{base}-{n}"
            n += 1
        used.add(key)
        entries = [
            (_safe_date(e.date, now_date), e.entry.strip()) for e in t.timeline if e.entry.strip()
        ]
        first = _safe_date(t.first_seen, now_date)
        last = max((d for d, _ in entries), default=first)
        built.append(
            _merge.Thread(
                key=key,
                title=t.title.strip(),
                section=t.section.strip() or _merge.FALLBACK_SECTION,
                first=first,
                last=last,
                current=t.current_state.strip(),
                entries=entries,
            )
        )
    return _merge.render_doc(built)


async def distill_events(
    history_text: str, backend: BaseLLMBackend, model: str | None, now_date: str
) -> str:
    """Distill N-days history into a canonical thread-model events.md (pure — no I/O)."""
    prompt = _DISTILL_USER.format(history=history_text)
    result = await backend.structured(prompt, _DistillResult, system=_DISTILL_SYSTEM, model=model)
    return render_threads(result.threads, now_date)


async def bootstrap_intent(
    store: KbStore,
    intent_id: int,
    history_text: str,
    backend: BaseLLMBackend,
    *,
    model: str | None,
    now: datetime | None = None,
    kind: str = "events",
) -> str:
    """Distill + write the initial events.md for an intent (the rebuild action)."""
    now_date = (now or datetime.now(UTC)).strftime("%Y-%m-%d")
    content = await distill_events(history_text, backend, model, now_date)
    await store.write(
        intent_id,
        content,
        kind=kind,
        identity=_INGEST_IDENTITY,
        message=f"rebuild intent-{intent_id} {kind} (cold-start distill)",
    )
    return content

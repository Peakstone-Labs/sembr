# SPDX-License-Identifier: Apache-2.0
"""KB cold-start distillation — the explicit "rebuild KB" path (design §7.1, O3).

Building the initial events.md is a one-off (per intent, on demand), so it can
afford a stronger model than the daily incremental merge. We do NOT auto-run this
when ``kb_enabled`` is toggled on (F1/O3) — only the explicit rebuild action calls
it, and an existing events.md is overwritten only with confirmation (enforced at
the API layer, P4).

The LLM returns a *structured* event list (validated), which we then render to the
canonical line format deterministically — same robustness argument as merge: the
model never hand-writes the ``<!--k:slug-->`` anchors / ISO dates, so the output is
always well-formed and immediately ingest-mergeable.
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

_DISTILL_SYSTEM = """你在为一个长期跟踪的主题建立「事件进展索引」的初始版本。给你过去 N 天的简报历史(每条带日期)。把它凝练成一组结构化事件。

要求:
- 把跨天出现的**同一事件/政策工具/数据指标/主体**合并为**一个事件**,记录首见日期与最新日期。
- 覆盖历史里反复出现或重要的事件;高频主题不得遗漏。
- 每个事件给:`title`(简短事件名,不带 markdown)、`section`(主题分节名,如 货币政策/信用与财政/增长与通胀数据/汇率与资本/官员表态 等)、`first_seen`/`last_seen`(**ISO 日期 YYYY-MM-DD**)、`state`(最新状态一句话,含关键数字)。

只输出 JSON,形如 {"events":[{"title":"...","section":"...","first_seen":"2026-06-01","last_seen":"2026-06-27","state":"..."}]}。不要任何解释。"""

_DISTILL_USER = """## 过去 N 天简报历史(每条带日期)

{history}

---

输出该主题的结构化事件列表(JSON)。"""


class _DistillEvent(BaseModel):
    title: str
    section: str
    first_seen: str
    last_seen: str
    state: str


class _DistillResult(BaseModel):
    events: list[_DistillEvent]


def _safe_date(raw: str, fallback: str) -> str:
    return raw if _ISO_RE.match(raw.strip()) else fallback


def render_events(events: list[_DistillEvent], now_date: str) -> str:
    """Render distilled events to canonical events.md, grouped by section.

    Keys are derived deterministically from the title (slug), de-duplicated with a
    numeric suffix — stable anchors the daily merge then matches against.
    """
    by_section: dict[str, list[_DistillEvent]] = {}
    order: list[str] = []
    for e in events:
        section = e.section.strip() or _merge.FALLBACK_SECTION
        if section not in by_section:
            by_section[section] = []
            order.append(section)
        by_section[section].append(e)

    used_keys: set[str] = set()
    out: list[str] = []
    for section in order:
        out.append(f"## {section}")
        for e in by_section[section]:
            # Content-derived fallback, not "event" (review 🔴-1): Chinese titles
            # all slugify to "" → without a content hash they'd collapse to
            # event/event-2/… positional keys that don't survive across days.
            base = _merge.slugify(e.title) or _merge.stable_fallback_key(e.title)
            key = base
            n = 2
            while key in used_keys:
                key = f"{base}-{n}"
                n += 1
            used_keys.add(key)
            first = _safe_date(e.first_seen, now_date)
            last = _safe_date(e.last_seen, now_date)
            out.append(_merge.build_line(key, e.title, first, last, e.state))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


async def distill_events(
    history_text: str,
    backend: BaseLLMBackend,
    model: str | None,
    now_date: str,
) -> str:
    """Distill N-days history prose into a canonical events.md (pure — no I/O)."""
    prompt = _DISTILL_USER.format(history=history_text)
    result = await backend.structured(prompt, _DistillResult, system=_DISTILL_SYSTEM, model=model)
    return render_events(result.events, now_date)


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
    """Distill + write the initial events.md for an intent (the rebuild action).

    Returns the written content. Caller (API) is responsible for the overwrite
    confirmation (O3) before invoking this.
    """
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

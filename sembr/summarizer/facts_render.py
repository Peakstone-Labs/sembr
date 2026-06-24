# SPDX-License-Identifier: Apache-2.0
"""Render pre-extracted structured facts into the {articles} slot for reduce.

Production form = VQP (verbatim quote per claim): each claim carries its verbatim
source-language ``quote`` and a general anti-hallucination preamble
(``PREAMBLE_V2``) is prepended. The injection probe's render_facts dropped quote
(it sat in ``_RESERVED``); production MUST render it — rendering the quote is
what made VQP beat the quote-less form (best citation precision + lowest
delta-mislabel rate).

Pure rendering, no I/O: ``map_for_reduce`` (mr_extract) produces the record
dicts; this module turns them + the spec into the LLM-facing facts text. The
renderer is spec-agnostic — it only reads the fixed Article shell (source_org /
thesis / claims[].section/text/quote + arbitrary spec fields) so any intent's
spec works with zero per-intent code.

``include_quote=False`` + ``PREAMBLE_V2_NOQUOTE`` is the budget-guard fallback:
when the VQP facts text would overflow the prompt budget, the
caller re-renders without quotes AND swaps to the no-quote preamble so the
preamble never claims "原文引语" that the facts block no longer contains.
"""

from __future__ import annotations

# _RESERVED: claim keys owned by the fixed shell or attached at runtime — never
# rendered as an extra "(field=value)" tag. Imported from spec (single source of
# truth) so the validator and the renderer can never drift on what's reserved.
from sembr.summarizer.spec import _RESERVED, GeneratedSpec

# General anti-hallucination preamble, prepended INSIDE the facts text (not the
# template layer) so it never conflicts with a user's custom instruction
# template. Core 6 clauses + 3 round-2 guards (over-attribution / language /
# delta-label) from the injection-probe conclusion — edits here change reduce
# output quality; keep in sync with that conclusion.
PREAMBLE_V2 = """## 注入说明（务必先读）

下方「Today's matched articles」槽位中的内容**不是文章全文**，而是已逐篇预抽取的**结构化事实（facts）**：每篇给出来源机构、论点（thesis），及若干条已分节、已带 `[N]` 来源编号的事实；部分事实附「原文」逐字引语（来源语言），仅供你核对数字与措辞。撰写简报时严格遵守：

- **只用下列已列出的事实/数字/机构/人名**；facts 未列出的一律不得出现（禁止凭常识或 history 补全今日事实）。
- **数字原样照抄** facts 与「原文」引语中的数值与口径，不得改写、换算或四舍五入。
- **每条事实句必须带 `[N]`**；多源支撑同一事实时列出全部 `[N]`；`[N]` 编号原样沿用 facts 标注，**禁止重新分配或臆造编号**。
- **不要超出 facts/引语实际支撑的范围做细粒度归属**：把某条具体数字或表态归给某个 `[N]` 前，确认该 `[N]` 的 facts/引语真含这条；拿不准就并列来源或弱化措辞，不要硬塞精确归属。
- **归属**：用 facts 给出的来源机构名（source_org）；标注 `attribution`/来源不明处，**禁止臆造具体机构或人名**。
- **预测 vs 事实**：facts 中标注 `is_projection=True` 的内容须框为「据 X 预计/预期」，不得当作既成事实陈述。
- **语言纪律**：「原文」引语只作你核对的参照，**正文一律用目标语言输出**。除模板明确许可的「英文一手源保留 1–2 句原文 + 中译」外，**不要把引语的原文整段或片段直接拼进正文**（尤其禁止英文片段嵌进中文句子）。
- **增量标签以 history 为准**：facts 是逐篇预抽取、**不含「该事件是否已在过去简报出现过」的信息**。一条事实即便在 facts 里看着是新的，只要 `history` 已经讲过该事件/表态，就标 `[持续]/[升级]/[降级]`，**不要标 `[新增]`**；`[新增]` 仅用于 history 中确实没有、今日首次出现的内容。
- **推断节（§5 资产影响、§6 反共识）为分析**，锚定上文事实与各篇 thesis 推理，**不加 `[N]`**。"""

# Budget-guard variant: PREAMBLE_V2 with the quote-referencing clauses removed
# (opening "原文引语" phrase + the 语言纪律 guard, which only exists for quotes)
# and the "原文引语" mention stripped from the number/attribution clauses. The
# delta-label and over-attribution guards stay — they are quote-INDEPENDENT, so
# falling back to the round-1 PREAMBLE_V1 (which lacks them) would be a
# regression. Used only when quotes are stripped to fit the budget.
PREAMBLE_V2_NOQUOTE = """## 注入说明（务必先读）

下方「Today's matched articles」槽位中的内容**不是文章全文**，而是已逐篇预抽取的**结构化事实（facts）**：每篇给出来源机构、论点（thesis），及若干条已分节、已带 `[N]` 来源编号的事实。撰写简报时严格遵守：

- **只用下列已列出的事实/数字/机构/人名**；facts 未列出的一律不得出现（禁止凭常识或 history 补全今日事实）。
- **数字原样照抄** facts 中的数值与口径，不得改写、换算或四舍五入。
- **每条事实句必须带 `[N]`**；多源支撑同一事实时列出全部 `[N]`；`[N]` 编号原样沿用 facts 标注，**禁止重新分配或臆造编号**。
- **不要超出 facts 实际支撑的范围做细粒度归属**：把某条具体数字或表态归给某个 `[N]` 前，确认该 `[N]` 的 facts 真含这条；拿不准就并列来源或弱化措辞，不要硬塞精确归属。
- **归属**：用 facts 给出的来源机构名（source_org）；标注 `attribution`/来源不明处，**禁止臆造具体机构或人名**。
- **预测 vs 事实**：facts 中标注 `is_projection=True` 的内容须框为「据 X 预计/预期」，不得当作既成事实陈述。
- **增量标签以 history 为准**：facts 是逐篇预抽取、**不含「该事件是否已在过去简报出现过」的信息**。一条事实即便在 facts 里看着是新的，只要 `history` 已经讲过该事件/表态，就标 `[持续]/[升级]/[降级]`，**不要标 `[新增]`**；`[新增]` 仅用于 history 中确实没有、今日首次出现的内容。
- **推断节（§5 资产影响、§6 反共识）为分析**，锚定上文事实与各篇 thesis 推理，**不加 `[N]`**。"""


def _sorted(records: list[dict]) -> list[dict]:
    """Order records by their 1-based recall index so [N] matches citation order."""
    return sorted(records, key=lambda r: r.get("index", 0))


def _org(rec: dict) -> str:
    return rec.get("source_org") or rec.get("source_name") or "?"


def _idx_block(records: list[dict]) -> str:
    """Article list — every cited [N] appears (incl. no-content/failed ones) so the
    reference set stays aligned with summary_history.citations."""
    lines = [
        f"[{r.get('index')}] {_org(r)} · {r.get('published_at') or '?'}" for r in _sorted(records)
    ]
    return "## 文章清单（索引 / 发布机构 / 发布时间）\n" + "\n".join(lines)


def _thesis_block(records: list[dict]) -> str | None:
    lines = [
        f"[{r.get('index')}] {_org(r)}: {r['thesis']}" for r in _sorted(records) if r.get("thesis")
    ]
    if not lines:
        return None
    return "## 各篇论点（thesis，供资产影响/反共识等推断节用）\n" + "\n".join(lines)


def _is_empty(v) -> bool:
    """Whether a claim-extra value should be omitted from the `(field=value)` tag.

    Drops only genuinely-empty values (None / empty string / empty container) and
    a bool ``False`` flag. Crucially does NOT drop numeric ``0`` / ``0.0`` — the
    old ``v in (None, "", [], {}, False)`` test ate them via ``0 == False``,
    silently swallowing a real "0" against the 数字原样照抄 contract. ``is False``
    (not ``== False``) keeps that bool-only without re-catching 0.
    """
    if v is None or v is False:
        return True
    return isinstance(v, str | list | dict) and len(v) == 0


def _fmt_value(v) -> str:
    """Render a claim-extra value compactly for the ``(field=value)`` tag.

    Scalars pass through as ``str``; a list joins its elements with ``; ``; a dict
    renders as space-separated ``key:value`` pairs (recursively). This keeps a
    structured field (e.g. ``metrics=[{name,value,unit,period}]``) from leaking a
    raw Python ``repr`` like ``{'name': 'CPI', ...}`` into the LLM-facing facts.
    Empty dict members are dropped, but numeric ``0`` is kept (数字原样照抄).
    """
    if isinstance(v, dict):
        return " ".join(
            f"{k}:{_fmt_value(val)}" for k, val in v.items() if val is not None and val != ""
        )
    if isinstance(v, list):
        return "; ".join(_fmt_value(x) for x in v)
    return str(v)


def _facts_block(records: list[dict], spec: GeneratedSpec, include_quote: bool) -> str:
    labels = {s.key: (s.label or s.key) for s in spec.sections}
    order = [s.key for s in spec.sections]

    buckets: dict[str, list[str]] = {}
    for r in _sorted(records):
        if r.get("no_relevant_content"):
            continue
        idx = r.get("index")
        for c in r.get("claims") or []:
            sec = c.get("section") or "other"
            extras = []
            for k, v in c.items():
                if k in _RESERVED or _is_empty(v):
                    continue
                extras.append(f"{k}={_fmt_value(v)}")
            tag = f"({', '.join(extras)}) " if extras else ""
            text = c.get("text", "") or ""
            line = f"  - [{idx}] {tag}{text}"
            if include_quote:
                q = (c.get("quote") or "").strip()
                # Skip the verbatim quote when it merely repeats the restatement:
                # an English source restated in English makes text == quote, so the
                # 〔原文〕 line would only waste prompt budget (no cross-check value).
                if q and q != text.strip():
                    line += f'\n      〔原文: "{q}"〕'
            buckets.setdefault(sec, []).append(line)

    out = ["## 结构化事实（已预抽取，按章节归类；每条带 [N]；仅据此撰写，勿编造未列出的数字/机构）"]
    for key in order + [k for k in buckets if k not in order]:
        if buckets.get(key):
            out.append(f"### {labels.get(key, key)}\n" + "\n".join(buckets[key]))
    return "\n\n".join(out)


def render_facts(
    records: list[dict],
    spec: GeneratedSpec,
    *,
    include_quote: bool = True,
    preamble: str | None = PREAMBLE_V2,
) -> str:
    """Assemble the facts text for the {articles} slot.

    Default = VQP: preamble + article list + thesis + section-bucketed claims with
    verbatim quote. Pass ``include_quote=False, preamble=PREAMBLE_V2_NOQUOTE`` for
    the budget-guard fallback so the preamble stays consistent with the (now
    quote-less) facts.

    ``records`` follow the map_for_reduce → render_facts contract:
    each has ``index`` (1-based recall position), optional source_org/source_name/
    published_at/thesis/no_relevant_content, and ``claims[]`` of
    {section, text, quote, + spec fields}.
    """
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    parts.append(_idx_block(records))
    tb = _thesis_block(records)
    if tb:
        parts.append(tb)
    parts.append(_facts_block(records, spec, include_quote))
    return "\n\n".join(parts)

# SPDX-License-Identifier: Apache-2.0
"""Aggregate summary_history rows into a single LLM summary.

Independent of :class:`SummaryPipeline` — does not read intent templates or
render Jinja.  The caller provides a raw prompt template with a ``{history}``
placeholder; this module replaces it with the joined row text and calls the LLM.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sembr.summarizer.pipeline import _BUDGET_SAFETY_RATIO

if TYPE_CHECKING:
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_PLACEHOLDER = "{history}"

# Strip [N] / [N,M] citation refs that leak from daily digest summaries into
# the aggregate prompt.  These refs are meaningless in aggregate context since
# we don't include the citation details, and the LLM may reproduce them.
_CITE_REF_RE = re.compile(r"\s*\[\d+(?:,\d+)*\]")


@dataclass
class AggregateResult:
    summary: str | None
    rows_total: int
    rows_used: int
    rows_dropped: int
    chars_budget: int
    chars_actual: int


def _render_prompt(template: str, joined_history: str) -> str:
    if _PLACEHOLDER not in template:
        raise MissingPlaceholderError(f"prompt must contain {_PLACEHOLDER!r} placeholder")
    return template.replace(_PLACEHOLDER, joined_history)


class MissingPlaceholderError(ValueError):
    """Raised when the prompt template is missing ``{history}``."""


def _select_rows_within_budget(rows: list[dict], budget: int) -> tuple[list[dict], int]:
    """Select newest-first rows whose formatted text fits within *budget* chars.

    Returns ``(selected, dropped)`` where *selected* is the prefix of *rows*
    (already in ``run_at DESC`` order) that fits and *dropped* is the count of
    rows excluded.  If even the newest single row overflows, *selected* is empty.
    """
    selected: list[dict] = []
    used = 0
    for r in rows:
        line = f"=== {r['run_at'][:10]} ===\n{r['summary']}"
        extra = len(line) + (2 if selected else 0)  # "\n\n" separator
        if used + extra > budget:
            break
        selected.append(r)
        used += extra
    return selected, len(rows) - len(selected)


async def aggregate_history(
    llm: BaseLLMBackend,
    prompt_template: str,
    rows: list[dict],
    *,
    safety_ratio: float = _BUDGET_SAFETY_RATIO,
) -> AggregateResult:
    """Run an LLM summary over *rows* using *prompt_template*.

    *rows* must be ordered ``run_at DESC`` (newest first) — the caller
    guarantees this (``list_summaries_between`` does it by default).
    """
    rows_total = len(rows)
    budget = int(llm.max_prompt_chars * safety_ratio)

    selected, rows_dropped = _select_rows_within_budget(rows, budget)
    rows_used = len(selected)

    if rows_used == 0 and rows_total > 0:
        # Even the newest single row overflows — prompt template is too long.
        overhead = len(prompt_template.replace(_PLACEHOLDER, ""))
        remaining = max(budget - overhead, 0)
        raise ValueError(
            f"prompt template too long: only {remaining} chars left for "
            f"history after fixed overhead"
        )

    if rows_used == 0:
        return AggregateResult(
            summary=None,
            rows_total=0,
            rows_used=0,
            rows_dropped=0,
            chars_budget=budget,
            chars_actual=0,
        )

    parts = [f"=== {r['run_at'][:10]} ===\n{_CITE_REF_RE.sub('', r['summary'])}" for r in selected]
    joined = "\n\n".join(parts)
    prompt = _render_prompt(prompt_template, joined)

    summary = await llm.summarize(prompt)
    return AggregateResult(
        summary=summary,
        rows_total=rows_total,
        rows_used=rows_used,
        rows_dropped=rows_dropped,
        chars_budget=budget,
        chars_actual=len(prompt),
    )

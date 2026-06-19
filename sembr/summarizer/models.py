# SPDX-License-Identifier: Apache-2.0
"""Summarizer domain types.

Intentionally dataclasses (not Pydantic) to match the Match dataclass pattern
in matcher/callback.py. Feature 7 can wrap these in Pydantic render models.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class Citation:
    article_id: str
    title: str
    url: str
    source: int  # feed_id; raw integer for downstream lookups
    published_at: str | None
    source_name: str | None = None  # resolved feed.name; None when feed deleted
    score: float | None = (
        None  # cosine similarity from the matcher; None when synthesised outside the match path
    )


@dataclass
class SummaryResult:
    intent_id: int
    summary: str
    # Canonical ordered list of cited articles. Position N (1-indexed) matches
    # the [N] reference the LLM may emit in `summary`. `primary` and
    # `other_sources` retained for legacy log_summaries / older callers; for
    # new consumers prefer `citations`.
    citations: list[Citation] = field(default_factory=list)
    primary: Citation | None = None
    other_sources: list[Citation] = field(default_factory=list)
    # Fire-time anchor set by compute_summary (unified effective_now from D12).
    # None when the caller hasn't threaded the anchor through (legacy paths);
    # non-None enables deterministic (intent_id, run_at) join between gate
    # audit logs and summary_history rows.
    run_at: str | None = None


PrePushHook = Callable[["SummaryResult"], Awaitable[bool]]
OnSummaryCallback = Callable[["SummaryResult"], Awaitable[None]]

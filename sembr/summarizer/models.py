"""Summarizer domain types.

Intentionally dataclasses (not Pydantic) to match the Match dataclass pattern
in matcher/callback.py. Feature 7 can wrap these in Pydantic render models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass
class Citation:
    article_id: str
    title: str
    url: str
    # MVP: feed_id integer. A future field `source_name: str | None` will carry
    # the human-readable label rather than overloading this field's type.
    source: int
    published_at: str | None


@dataclass
class SummaryResult:
    intent_id: int
    summary: str
    primary: Citation
    other_sources: list[Citation] = field(default_factory=list)


PrePushHook = Callable[["SummaryResult"], Awaitable[bool]]
OnSummaryCallback = Callable[["SummaryResult"], Awaitable[None]]

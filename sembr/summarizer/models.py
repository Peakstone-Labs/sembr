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
    source: int  # feed_id; raw integer for downstream lookups
    published_at: str | None
    source_name: str | None = None  # resolved feed.name; None when feed deleted


@dataclass
class SummaryResult:
    intent_id: int
    summary: str
    primary: Citation
    other_sources: list[Citation] = field(default_factory=list)


PrePushHook = Callable[["SummaryResult"], Awaitable[bool]]
OnSummaryCallback = Callable[["SummaryResult"], Awaitable[None]]

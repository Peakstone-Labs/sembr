"""Matcher callback types and default implementation.

D13: on_match is registered on app.state so Feature 6/7 can replace it at runtime.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class Match:
    intent_id: int
    article_id: str  # UUID string matching Qdrant point ID
    score: float
    payload: dict


OnMatchCallback = Callable[[list[Match]], Awaitable[None]]


async def log_matches(matches: list[Match]) -> None:
    """Default on_match: one summary INFO line per tick, per intent (R2).

    Intentionally does not write to disk or any table — purely a log sink
    until Feature 6/7 replaces this with a real notifier.
    """
    if not matches:
        return
    intent_id = matches[0].intent_id
    article_ids = [m.article_id for m in matches]
    logger.info("on_match intent_id=%d matched %d articles: %s", intent_id, len(matches), article_ids)
    for m in matches:
        logger.debug(
            "  article_id=%s score=%.4f title=%r",
            m.article_id,
            m.score,
            m.payload.get("title", ""),
        )

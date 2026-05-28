# SPDX-License-Identifier: Apache-2.0
"""Matcher callback types and default implementation.

``on_match`` is registered on ``app.state`` so the lifespan can replace the
default log-only sink with the real summarizer pipeline at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Match:
    intent_id: int
    article_id: str  # UUID string matching Qdrant point ID
    score: float
    payload: dict


OnMatchCallback = Callable[[list[Match]], Awaitable[None]]


async def log_matches(matches: list[Match]) -> None:
    """Default on_match: one summary INFO line per tick, per intent.

    Intentionally does not write to disk or any table — purely a log sink
    until Feature 6/7 replaces this with a real notifier.
    """
    if not matches:
        return
    intent_id = matches[0].intent_id
    article_ids = [m.article_id for m in matches]
    logger.info(
        "on_match intent_id=%d matched %d articles: %s", intent_id, len(matches), article_ids
    )
    for m in matches:
        logger.debug(
            "  article_id=%s score=%.4f title=%r",
            m.article_id,
            m.score,
            m.payload.get("title", ""),
        )

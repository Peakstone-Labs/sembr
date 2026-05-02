"""Event-driven intent matching: in-process cosine scoring against cached intent vectors.

D11: event_match_batch — called after each Qdrant upsert in embedder_worker.
D18: no match_seen writes on this path.
Risk 7: top-level try/except — event path failure must not abort ingestion.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiosqlite

from sembr.matcher.callback import Match

if TYPE_CHECKING:
    from sembr.matcher.event_cache import EventIntentCache

logger = logging.getLogger(__name__)


def _dot(a: list[float], b: list[float]) -> float:
    """Pure-Python dot product for unit-normalized 1024-dim BGE-M3 vectors.

    BGE-M3 outputs are L2-normalized so dot == cosine similarity.
    No numpy dependency (not in pyproject.toml).
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(a, b))


async def event_match_batch(
    app,
    points: list,
    conn: aiosqlite.Connection,
) -> None:
    """Score each point against all cached event-mode intents; absorb hits into buffer.

    Risk 7: never raises — any exception is logged as WARNING so the caller
    (embedder_worker) continues with delete_pending regardless of event-path health.
    """
    try:
        await _event_match_batch_inner(app, points, conn)
    except Exception as exc:
        logger.warning("event_match_batch failed (ingestion unaffected): %s", exc, exc_info=True)


async def _event_match_batch_inner(
    app,
    points: list,
    conn: aiosqlite.Connection,
) -> None:
    from sembr.matcher.event_buffer import absorb, flush  # noqa: PLC0415

    cache: "EventIntentCache" = app.state.event_intent_cache
    if len(cache) == 0:
        return

    # Collect (article_vector, payload) from each point
    article_entries: list[tuple[list[float], dict, str]] = []
    for pt in points:
        vec = pt.vector if not isinstance(pt.vector, dict) else None
        if vec is None:
            continue
        article_entries.append((list(vec), pt.payload or {}, str(pt.id)))

    if not article_entries:
        return

    for intent_id, entry in cache.items():
        intent_vec = entry.vector
        hits: list[Match] = []

        for article_vec, payload, article_id in article_entries:
            # feed_filter: skip if article's feed_id not in allowed set
            if entry.feed_filter_ids is not None:
                feed_id = payload.get("feed_id")
                if feed_id not in entry.feed_filter_ids:
                    continue

            score = _dot(intent_vec, article_vec)
            if score >= entry.threshold:
                hits.append(
                    Match(
                        intent_id=intent_id,
                        article_id=article_id,
                        score=score,
                        payload=payload,
                    )
                )

        if not hits:
            continue

        logger.debug(
            "event_match: intent_id=%d batch_hits=%d",
            intent_id, len(hits),
        )

        should_flush = await absorb(conn, intent_id, hits, entry.schedule)
        if should_flush:
            logger.info(
                "event_match: intent_id=%d reached trigger_count=%d → flushing",
                intent_id, entry.schedule.trigger_count,
            )
            await flush(conn, app, intent_id)

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
from sembr.vector_store.qdrant import extract_point_vector

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

    # _dot computes cosine via plain dot product, which is only correct when the
    # embedder returns unit-norm vectors. Verifying once per batch is cheaper than
    # adding the L2 normalization to the hot loop, and refusing to score is safer
    # than producing silently wrong similarities.
    embedder = getattr(app.state, "embedder", None)
    if embedder is not None and not embedder.is_unit_normalized:
        logger.warning(
            "event_match: embedder %r is not unit-normalized; refusing to score "
            "(would yield wrong similarities). Switch to a normalized backend or "
            "implement an L2-aware matcher.",
            type(embedder).__name__,
        )
        return

    # Collect (article_vector, payload) from each point
    article_entries: list[tuple[list[float], dict, str]] = []
    for pt in points:
        vec = extract_point_vector(pt)
        if vec is None:
            continue
        article_entries.append((vec, pt.payload or {}, str(pt.id)))

    if not article_entries:
        return

    for intent_id, entry in cache.items():
        slot_vecs = list(entry.vectors.values())
        if not slot_vecs:
            continue
        hits: list[Match] = []

        for article_vec, payload, article_id in article_entries:
            # feed_filter: skip if article's feed_id not in allowed set
            if entry.feed_filter_ids is not None:
                feed_id = payload.get("feed_id")
                if feed_id not in entry.feed_filter_ids:
                    continue

            # D12: score each slot independently, keep max (dedupe on article side)
            score = max(_dot(sv, article_vec) for sv in slot_vecs)
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

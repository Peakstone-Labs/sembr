"""Per-intent scan logic: one APScheduler tick = one call to run_intent_scan.

Flow per tick (from design):
  1. Guard: embedder not ready → skip (D6)
  2. Load Intent from SQLite; skip if missing/disabled (race cover)
  3. Retrieve intent vector from Qdrant intents_current (B1)
  4. Search news_current with score_threshold + Range(ingested_at_ts) filter (C1)
  5. Filter disabled articles (D20 — always-True at MVP, retention hook)
  6. INSERT OR IGNORE into match_seen; RETURNING gives new article_ids (D11)
  7. Build Match list for new article_ids
  8. If non-empty: await app.state.on_match(matches) (D12, D13)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sembr.db.intents import get_intent
from sembr.db.match_seen import insert_unseen_returning_new
from sembr.db.sqlite import get_conn
from sembr.matcher.callback import Match

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_NEWS_ALIAS = "news_current"
_INTENTS_ALIAS = "intents_current"
# Upper bound on search results per tick. 100 is generous for MVP intent counts.
_SEARCH_LIMIT = 100


async def run_intent_scan(intent_id: int, app: "FastAPI") -> None:
    # D6: skip tick if embedder not loaded (startup race / reload window)
    if not app.state.embedder.is_loaded:
        logger.warning("intent_id=%d scan skipped: embedder not ready", intent_id)
        return

    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None or not intent.enabled:
        # Race cover: intent deleted or disabled between job registration and tick
        logger.debug("intent_id=%d scan skipped: intent missing or disabled", intent_id)
        return

    qdrant_client = app.state.qdrant.client

    # D7: any Qdrant error skips this tick cleanly; coalesce=True prevents accumulation
    try:
        from qdrant_client.models import FieldCondition, Filter, Range  # noqa: PLC0415

        # B1: retrieve intent vector fresh per tick; avoids stale-cache inconsistency
        points = await qdrant_client.retrieve(
            collection_name=_INTENTS_ALIAS,
            ids=[intent_id],
            with_vectors=True,
        )
        if not points:
            # Possible if DELETE /intents partially failed (Qdrant deleted, SQLite delete
            # failed). The intent row still exists, but its vector is gone. Disable or
            # re-create the intent to stop this warning.
            logger.warning(
                "intent_id=%d has no vector in Qdrant (possible inconsistency); "
                "disable or re-create this intent to stop repeated warnings",
                intent_id,
            )
            return
        intent_vector = points[0].vector

        lookback_cutoff_ts = (
            int(datetime.now(timezone.utc).timestamp()) - intent.lookback_window_seconds
        )
        # qdrant-client 1.10+ removed search() in favour of query_points()
        response = await qdrant_client.query_points(
            collection_name=_NEWS_ALIAS,
            query=intent_vector,
            score_threshold=intent.threshold,
            limit=_SEARCH_LIMIT,
            query_filter=Filter(
                must=[
                    FieldCondition(
                        key="ingested_at_ts",
                        range=Range(gte=lookback_cutoff_ts),
                    )
                ]
            ),
        )
        results = response.points
        logger.debug(
            "intent_id=%d scan: qdrant returned %d results (threshold=%.2f, lookback_cutoff_ts=%d)",
            intent_id,
            len(results),
            intent.threshold,
            lookback_cutoff_ts,
        )

        # Diagnostic probe: when normal query returns nothing, run two fallback queries
        # to distinguish "time filter too narrow" from "threshold too high".
        if not results:
            _probe_no_time = await qdrant_client.query_points(
                collection_name=_NEWS_ALIAS,
                query=intent_vector,
                score_threshold=intent.threshold,
                limit=3,
            )
            _probe_no_thresh = await qdrant_client.query_points(
                collection_name=_NEWS_ALIAS,
                query=intent_vector,
                score_threshold=0.0,
                limit=3,
                query_filter=Filter(
                    must=[
                        FieldCondition(
                            key="ingested_at_ts",
                            range=Range(gte=lookback_cutoff_ts),
                        )
                    ]
                ),
            )
            logger.info(
                "intent_id=%d DIAG: no-time-filter hits=%d, no-threshold hits=%d (best_score=%.4f)",
                intent_id,
                len(_probe_no_time.points),
                len(_probe_no_thresh.points),
                _probe_no_thresh.points[0].score if _probe_no_thresh.points else 0.0,
            )
            if _probe_no_thresh.points:
                logger.info(
                    "intent_id=%d DIAG best in-window article: score=%.4f title=%r",
                    intent_id,
                    _probe_no_thresh.points[0].score,
                    (_probe_no_thresh.points[0].payload or {}).get("title", "")[:100],
                )
            if _probe_no_time.points:
                logger.info(
                    "intent_id=%d DIAG best any-time article: score=%.4f title=%r ingested_at_ts=%s",
                    intent_id,
                    _probe_no_time.points[0].score,
                    (_probe_no_time.points[0].payload or {}).get("title", "")[:100],
                    (_probe_no_time.points[0].payload or {}).get("ingested_at_ts"),
                )

        if results:
            top = results[0]
            logger.debug(
                "intent_id=%d top hit: score=%.4f id=%s title=%r",
                intent_id,
                top.score,
                top.id,
                (top.payload or {}).get("title", "")[:80],
            )
    except Exception as exc:
        logger.warning("intent_id=%d scan Qdrant error, skipping tick: %s", intent_id, exc)
        return

    # D20: exclude articles with enabled=False (retention hook; currently always True
    # because news points don't carry an 'enabled' payload field at MVP)
    hits = [r for r in results if r.payload.get("enabled", True)]
    logger.info(
        "intent_id=%d scan: %d Qdrant hits → %d after enabled-filter",
        intent_id,
        len(results),
        len(hits),
    )
    if not hits:
        return

    article_ids = [str(r.id) for r in hits]
    try:
        new_article_ids = await insert_unseen_returning_new(conn, intent_id, article_ids)
    except Exception as exc:
        # FK violation if intent was deleted mid-scan (between load and insert).
        # Abort this tick cleanly; next tick either won't fire (job unregistered) or
        # will hit the get_intent guard above.
        logger.warning(
            "intent_id=%d match_seen insert failed (%s, intent may have been deleted): %s",
            intent_id,
            type(exc).__name__,
            exc,
        )
        return
    if not new_article_ids:
        return

    id_to_hit = {str(r.id): r for r in hits}
    matches = [
        Match(
            intent_id=intent_id,
            article_id=aid,
            score=id_to_hit[aid].score,
            payload=id_to_hit[aid].payload,
        )
        for aid in new_article_ids
    ]

    # R5: guard against on_match being None during startup race (scheduler.start() fires
    # before app.state is fully visible in some edge cases; belt-and-suspenders check)
    callback = app.state.on_match
    if callback is None:
        logger.error("intent_id=%d on_match is None, skipping notification", intent_id)
        return

    try:
        await callback(matches)
    except Exception as exc:
        # E1: match_seen already committed; on_match failure = silent loss for this tick.
        # The callback implementation is responsible for its own error handling.
        logger.error("intent_id=%d on_match raised: %s", intent_id, exc)

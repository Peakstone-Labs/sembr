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
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiosqlite

from sembr.db.intents import get_intent
from sembr.db.match_seen import insert_unseen_returning_new
from sembr.db.sqlite import get_conn
from sembr.matcher.callback import Match
from sembr.vector_store.intents import ALIAS_NAME as _INTENTS_ALIAS
from sembr.vector_store.news import ALIAS_NAME as _NEWS_ALIAS
from sembr.vector_store.qdrant import extract_point_vector

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sembr.models import Intent

logger = logging.getLogger(__name__)

# Upper bound on search results per tick. 100 is generous for MVP intent counts.
_SEARCH_LIMIT = 100


@dataclass
class ScanOptions:
    """Options passed to scan_once; controls which articles to match and how to record them."""

    lookback_seconds: int
    threshold: float
    skip_seen: bool  # True → filter out already-seen articles before returning
    feed_ids: list[int] | None  # None=all feeds; []=no feeds (short-circuits to [])
    write_match_seen: bool  # True → insert hits into match_seen; False → fire path
    # D-A6 / D12: cron path keeps the silent-skip-tick contract (False); sync
    # external fire endpoint sets True so a Qdrant outage surfaces as 500 rather
    # than masquerading as 0 hits.
    propagate_qdrant_errors: bool = False


async def scan_once(
    intent: "Intent",
    options: ScanOptions,
    conn: aiosqlite.Connection,
    qdrant_client,
) -> list[Match]:
    """Core scan shared by scheduled ticks and fire requests.

    Returns the list of matches to notify. When write_match_seen=True and
    skip_seen=True, only truly new (not-yet-seen) matches are returned.
    When write_match_seen=False (fire path), all hits are returned regardless
    of match_seen state.
    """
    # R10: empty feed_ids set matches nothing — short-circuit before any Qdrant call
    if options.feed_ids is not None and len(options.feed_ids) == 0:
        logger.debug("intent_id=%d scan_once short-circuit: feed_ids=[]", intent.id)
        return []

    try:
        from qdrant_client.models import FieldCondition, Filter, MatchAny, Range  # noqa: PLC0415

        # B1: retrieve intent vector fresh per call; avoids stale-cache inconsistency
        points = await qdrant_client.retrieve(
            collection_name=_INTENTS_ALIAS,
            ids=[intent.id],
            with_vectors=True,
        )
        if not points:
            logger.warning(
                "intent_id=%d has no vector in Qdrant (possible inconsistency); "
                "disable or re-create this intent to stop repeated warnings",
                intent.id,
            )
            return []
        intent_vector = extract_point_vector(points[0])
        if intent_vector is None:
            logger.warning(
                "intent_id=%d Qdrant point has no resolvable vector "
                "(named-vector layout?); skipping scan tick",
                intent.id,
            )
            return []

        lookback_cutoff_ts = (
            int(datetime.now(timezone.utc).timestamp()) - options.lookback_seconds
        )

        must_conditions: list = [
            FieldCondition(
                key="ingested_at_ts",
                range=Range(gte=lookback_cutoff_ts),
            )
        ]
        if options.feed_ids is not None:
            must_conditions.append(
                FieldCondition(
                    key="feed_id",
                    match=MatchAny(any=options.feed_ids),
                )
            )

        # qdrant-client 1.10+ removed search() in favour of query_points()
        response = await qdrant_client.query_points(
            collection_name=_NEWS_ALIAS,
            query=intent_vector,
            score_threshold=options.threshold,
            limit=_SEARCH_LIMIT,
            query_filter=Filter(must=must_conditions),
        )
        results = response.points
        logger.debug(
            "intent_id=%d scan_once: qdrant returned %d results "
            "(threshold=%.2f, lookback_cutoff_ts=%d, feed_ids=%s)",
            intent.id,
            len(results),
            options.threshold,
            lookback_cutoff_ts,
            options.feed_ids,
        )

        # Diagnostic probe: when normal query returns nothing, run two fallback queries
        # to distinguish "time filter too narrow" from "threshold too high".
        # Gated behind SEMBR_DEBUG_MATCHER because empty results are the common case
        # under cron mode — running two extra Qdrant queries per intent per tick
        # multiplies load by ~3× when most intents have nothing to match this window.
        if not results and os.environ.get("SEMBR_DEBUG_MATCHER"):
            _feed_cond = (
                [FieldCondition(key="feed_id", match=MatchAny(any=options.feed_ids))]
                if options.feed_ids is not None
                else []
            )
            _probe_no_time = await qdrant_client.query_points(
                collection_name=_NEWS_ALIAS,
                query=intent_vector,
                score_threshold=options.threshold,
                limit=3,
                query_filter=Filter(must=_feed_cond) if _feed_cond else None,
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
                        ),
                        *_feed_cond,
                    ]
                ),
            )
            logger.info(
                "intent_id=%d DIAG: no-time-filter hits=%d, no-threshold hits=%d (best_score=%.4f)",
                intent.id,
                len(_probe_no_time.points),
                len(_probe_no_thresh.points),
                _probe_no_thresh.points[0].score if _probe_no_thresh.points else 0.0,
            )
            if _probe_no_thresh.points:
                logger.info(
                    "intent_id=%d DIAG best in-window article: score=%.4f title=%r",
                    intent.id,
                    _probe_no_thresh.points[0].score,
                    (_probe_no_thresh.points[0].payload or {}).get("title", "")[:100],
                )
            if _probe_no_time.points:
                logger.info(
                    "intent_id=%d DIAG best any-time article: score=%.4f title=%r ingested_at_ts=%s",
                    intent.id,
                    _probe_no_time.points[0].score,
                    (_probe_no_time.points[0].payload or {}).get("title", "")[:100],
                    (_probe_no_time.points[0].payload or {}).get("ingested_at_ts"),
                )

        if results:
            top = results[0]
            logger.debug(
                "intent_id=%d top hit: score=%.4f id=%s title=%r",
                intent.id,
                top.score,
                top.id,
                (top.payload or {}).get("title", "")[:80],
            )
    except Exception as exc:
        if options.propagate_qdrant_errors:
            raise
        logger.warning("intent_id=%d scan_once Qdrant error: %s", intent.id, exc)
        return []

    # D20: exclude articles with enabled=False (retention hook; currently always True
    # because news points don't carry an 'enabled' payload field at MVP)
    hits = [r for r in results if (r.payload or {}).get("enabled", True)]
    logger.info(
        "intent_id=%d scan_once: %d Qdrant hits → %d after enabled-filter",
        intent.id,
        len(results),
        len(hits),
    )
    if not hits:
        return []

    article_ids = [str(r.id) for r in hits]
    id_to_hit = {str(r.id): r for r in hits}

    if options.write_match_seen:
        try:
            if options.skip_seen:
                # Read + write: filter out already-seen, return only new
                new_article_ids = await insert_unseen_returning_new(conn, intent.id, article_ids)
            else:
                # Write-only: record all hits but return all of them (notify every time)
                await insert_unseen_returning_new(conn, intent.id, article_ids)
                new_article_ids = article_ids
        except Exception as exc:
            # FK violation if intent was deleted mid-scan.
            logger.warning(
                "intent_id=%d match_seen insert failed (%s, intent may have been deleted): %s",
                intent.id,
                type(exc).__name__,
                exc,
            )
            return []
    else:
        # Fire path: never touch match_seen, return all hits
        new_article_ids = article_ids

    if not new_article_ids:
        return []

    return [
        Match(
            intent_id=intent.id,
            article_id=aid,
            score=id_to_hit[aid].score,
            payload=id_to_hit[aid].payload,
        )
        for aid in new_article_ids
    ]


async def run_intent_scan(intent_id: int, app: "FastAPI") -> None:
    # The scan path reads pre-computed vectors from Qdrant; it does not call the
    # embedder. An earlier version skipped the tick when `embedder.is_loaded` was
    # False, which caused permanent silent misses when the SiliconFlow startup
    # probe failed (load() does not retry, so is_loaded stays False until restart).
    # We deliberately do not log embedder status here — the /health endpoint and
    # the dashboard already surface it once, and a per-tick warning would be pure
    # log noise on a degraded but still-functional matcher.
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None or not intent.enabled:
        # Race cover: intent deleted or disabled between job registration and tick
        logger.debug("intent_id=%d scan skipped: intent missing or disabled", intent_id)
        return

    from sembr.models import CronSchedule  # noqa: PLC0415

    if not isinstance(intent.schedule, CronSchedule):
        logger.warning(
            "intent_id=%d run_intent_scan called for non-cron schedule mode=%r; skipping",
            intent_id,
            intent.schedule.mode,
        )
        return

    qdrant_client = app.state.qdrant.client

    options = ScanOptions(
        lookback_seconds=intent.schedule.lookback_seconds,
        threshold=intent.threshold,
        skip_seen=intent.schedule.skip_seen,
        feed_ids=intent.feed_filter.ids if intent.feed_filter else None,
        write_match_seen=True,
    )

    matches = await scan_once(intent, options, conn, qdrant_client)
    if not matches:
        return

    # R5: guard against on_match being None during startup race
    callback = app.state.on_match
    if callback is None:
        logger.error("intent_id=%d on_match is None, skipping notification", intent_id)
        return

    try:
        await callback(matches)
    except Exception as exc:
        # E1: match_seen already committed; on_match failure = silent loss for this tick.
        logger.error("intent_id=%d on_match raised: %s", intent_id, exc)

"""Event buffer: absorb + flush business logic for event-driven intent matching.

D13: absorb() — single transaction; merges batch_groups into event_pending.
D14: flush() — BEGIN/SELECT/DELETE/COMMIT then on_match (commit-before-notify pattern).
D15: sweep_timed_out() — called by event_y_sweeper APScheduler job every 30s.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import aiosqlite

from sembr.summarizer.grouping import _UnionFind, _normalize

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.matcher.event_cache import EventIntentCache
    from sembr.models import EventSchedule

logger = logging.getLogger(__name__)

_MERGE_THRESHOLD = 0.85


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _group_batch(
    batch_matches: list["Match"],
) -> list[list["Match"]]:
    """Cluster this batch by title similarity (reuses GroupingStep logic)."""
    n = len(batch_matches)
    if n == 0:
        return []
    normalized = [_normalize(m.payload.get("title", "")) for m in batch_matches]
    uf = _UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if SequenceMatcher(None, normalized[i], normalized[j]).ratio() >= _MERGE_THRESHOLD:
                uf.union(i, j)
    groups: dict[int, list[Match]] = {}
    for i, m in enumerate(batch_matches):
        groups.setdefault(uf.find(i), []).append(m)
    return list(groups.values())


async def absorb(
    conn: aiosqlite.Connection,
    intent_id: int,
    batch_matches: list["Match"],
    schedule: "EventSchedule",
) -> bool:
    """Merge batch_matches into event_pending; return True if flush should follow.

    All DB writes in a single transaction. Article-level dedup inside each group
    prevents double-counting on batch retry (Risk 2).
    """
    if not batch_matches:
        return False

    batch_groups = _group_batch(batch_matches)

    # Load existing groups for this intent
    async with conn.execute(
        "SELECT group_id, rep_title_norm, members_json FROM event_pending WHERE intent_id=?",
        (intent_id,),
    ) as cur:
        existing_rows = await cur.fetchall()

    # Represent existing groups as mutable list of (group_id, rep_title_norm, members_list)
    existing: list[tuple[int, str, list[dict]]] = []
    for gid, rep_norm, members_raw in existing_rows:
        existing.append((gid, rep_norm, json.loads(members_raw)))

    # Compute next available group_id
    next_gid = (max((g[0] for g in existing), default=-1) + 1)

    now = _now_utc()

    for batch_group in batch_groups:
        rep = batch_group[0]
        rep_norm = _normalize(rep.payload.get("title", ""))

        # Try to merge into an existing group
        merged = False
        for idx, (gid, ex_norm, ex_members) in enumerate(existing):
            if SequenceMatcher(None, rep_norm, ex_norm).ratio() >= _MERGE_THRESHOLD:
                # Append new members (dedup by article_id)
                existing_ids = {m["article_id"] for m in ex_members}
                new_members = [
                    {
                        "article_id": m.article_id,
                        "title": m.payload.get("title", ""),
                        "url": m.payload.get("url", ""),
                        "body": m.payload.get("body", ""),
                        "feed_id": m.payload.get("feed_id"),
                        "published_at": m.payload.get("published_at"),
                        "score": m.score,
                    }
                    for m in batch_group
                    if m.article_id not in existing_ids
                ]
                if new_members:
                    updated_members = ex_members + new_members
                    await conn.execute(
                        "UPDATE event_pending SET members_json=? WHERE intent_id=? AND group_id=?",
                        (json.dumps(updated_members, ensure_ascii=False), intent_id, gid),
                    )
                    existing[idx] = (gid, ex_norm, updated_members)
                merged = True
                break

        if not merged:
            members = [
                {
                    "article_id": m.article_id,
                    "title": m.payload.get("title", ""),
                    "url": m.payload.get("url", ""),
                    "body": m.payload.get("body", ""),
                    "feed_id": m.payload.get("feed_id"),
                    "published_at": m.payload.get("published_at"),
                    "score": m.score,
                }
                for m in batch_group
            ]
            rep_article_id = rep.article_id
            await conn.execute(
                "INSERT INTO event_pending "
                "(intent_id, group_id, rep_article_id, rep_title_norm, members_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    intent_id,
                    next_gid,
                    rep_article_id,
                    rep_norm,
                    json.dumps(members, ensure_ascii=False),
                    now,
                ),
            )
            existing.append((next_gid, rep_norm, members))
            next_gid += 1

    await conn.commit()

    group_count = len(existing)
    should_flush = group_count >= schedule.trigger_count
    logger.debug(
        "absorb intent_id=%d groups=%d trigger_count=%d should_flush=%s",
        intent_id, group_count, schedule.trigger_count, should_flush,
    )
    return should_flush


async def flush(conn: aiosqlite.Connection, app, intent_id: int) -> None:
    """Drain event_pending for intent_id → call on_match.

    D14: DELETE committed before on_match is awaited. on_match failure is logged
    but NOT re-raised — buffer is already cleared (E1 contract, same as cron path).
    """
    from sembr.matcher.callback import Match  # noqa: PLC0415

    async with conn.execute(
        "SELECT members_json FROM event_pending WHERE intent_id=? ORDER BY group_id",
        (intent_id,),
    ) as cur:
        rows = await cur.fetchall()

    if not rows:
        return

    matches: list[Match] = []
    for (members_raw,) in rows:
        for member in json.loads(members_raw):
            matches.append(
                Match(
                    intent_id=intent_id,
                    article_id=member["article_id"],
                    score=member["score"],
                    payload={
                        "title": member.get("title", ""),
                        "url": member.get("url", ""),
                        "body": member.get("body", ""),
                        "feed_id": member.get("feed_id"),
                        "published_at": member.get("published_at"),
                    },
                )
            )

    await conn.execute(
        "DELETE FROM event_pending WHERE intent_id=?",
        (intent_id,),
    )
    await conn.commit()

    on_match = getattr(app.state, "on_match", None)
    if on_match is None:
        logger.debug("flush intent_id=%d: no on_match handler registered", intent_id)
        return

    try:
        await on_match(matches)
        logger.info("flush intent_id=%d: pushed %d matches", intent_id, len(matches))
    except Exception as exc:
        logger.warning(
            "flush intent_id=%d: on_match raised (buffer already cleared): %s",
            intent_id, exc, exc_info=True,
        )


async def sweep_timed_out(
    conn: aiosqlite.Connection,
    app,
    event_intent_cache: "EventIntentCache",
) -> None:
    """Flush intents whose oldest buffered group has exceeded max_wait_seconds.

    Called every 30s by event_y_sweeper APScheduler job (D15).
    Each intent failure is isolated — one bad flush does not abort others.
    """
    now = datetime.now(timezone.utc)

    async with conn.execute(
        "SELECT intent_id, MIN(created_at) AS earliest "
        "FROM event_pending GROUP BY intent_id"
    ) as cur:
        rows = await cur.fetchall()

    for intent_id, earliest_str in rows:
        entry = event_intent_cache.get(intent_id)
        if entry is None:
            logger.warning(
                "sweep: intent_id=%d has buffered events but no cache entry; flushing anyway",
                intent_id,
            )
            max_wait = 1800
        else:
            max_wait = entry.schedule.max_wait_seconds

        try:
            earliest = datetime.fromisoformat(earliest_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            logger.warning("sweep: intent_id=%d unparseable created_at=%r", intent_id, earliest_str)
            continue

        age_seconds = (now - earliest).total_seconds()
        if age_seconds >= max_wait:
            logger.info(
                "sweep: intent_id=%d oldest_group_age=%.0fs >= max_wait=%ds → flushing",
                intent_id, age_seconds, max_wait,
            )
            try:
                await flush(conn, app, intent_id)
            except Exception as exc:
                logger.warning("sweep: flush intent_id=%d failed: %s", intent_id, exc, exc_info=True)

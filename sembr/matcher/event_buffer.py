# SPDX-License-Identifier: Apache-2.0
"""Event buffer: absorb + flush business logic for event-driven intent matching.

Three entry points:

* ``absorb()`` — explicit BEGIN IMMEDIATE transaction; merges batch_groups
  into the ``event_pending`` table.
* ``flush()`` — DELETE ... RETURNING (atomic read+delete); on_match is called
  after the transaction commits.
* ``sweep_timed_out()`` — called by event_y_sweeper every 30 s.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

import aiosqlite

from sembr.summarizer.grouping import GroupingStep, normalize

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.matcher.event_cache import EventIntentCache
    from sembr.models import EventSchedule

logger = logging.getLogger(__name__)

_MERGE_THRESHOLD = 0.85
_GROUPER = GroupingStep(threshold=_MERGE_THRESHOLD)


def _now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


async def absorb(
    conn: aiosqlite.Connection,
    intent_id: int,
    batch_matches: list[Match],
    schedule: EventSchedule,
) -> bool:
    """Merge batch_matches into event_pending; return True if flush should follow.

    Uses BEGIN IMMEDIATE so the SELECT + N UPDATEs/INSERTs are atomic.
    Article-level dedup inside each group prevents double-counting on batch retry (Risk 2).
    """
    if not batch_matches:
        return False

    batch_groups = _GROUPER.group(batch_matches)

    await conn.execute("BEGIN IMMEDIATE")
    try:
        # Load existing groups for this intent (inside the write transaction)
        async with conn.execute(
            "SELECT group_id, rep_title_norm, members_json FROM event_pending WHERE intent_id=?",
            (intent_id,),
        ) as cur:
            existing_rows = await cur.fetchall()

        # Mutable list of (group_id, rep_title_norm, members_list)
        existing: list[tuple[int, str, list[dict]]] = []
        for gid, rep_norm, members_raw in existing_rows:
            existing.append((gid, rep_norm, json.loads(members_raw)))

        next_gid = max((g[0] for g in existing), default=-1) + 1
        now = _now_utc()

        for batch_group in batch_groups:
            rep = batch_group[0]
            rep_norm = normalize(rep.payload.get("title", ""))

            # NOTE: first-merge-wins by group_id insertion order — intentional.
            # If rep_norm is ≥0.85 similar to two existing groups they were not
            # transitively merged at absorb time (only batch-internal merging uses
            # UnionFind). Merging into the first matching group is the correct
            # conservative choice; cross-group dedup happens implicitly via flush.
            merged = False
            for idx, (gid, ex_norm, ex_members) in enumerate(existing):
                if SequenceMatcher(None, rep_norm, ex_norm).ratio() >= _MERGE_THRESHOLD:
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
                            "UPDATE event_pending SET members_json=? "
                            "WHERE intent_id=? AND group_id=?",
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
                await conn.execute(
                    "INSERT INTO event_pending "
                    "(intent_id, group_id, rep_article_id, rep_title_norm, members_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        intent_id,
                        next_gid,
                        rep.article_id,
                        rep_norm,
                        json.dumps(members, ensure_ascii=False),
                        now,
                    ),
                )
                existing.append((next_gid, rep_norm, members))
                next_gid += 1

        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    group_count = len(existing)
    should_flush = group_count >= schedule.trigger_count
    logger.debug(
        "absorb intent_id=%d groups=%d trigger_count=%d should_flush=%s",
        intent_id,
        group_count,
        schedule.trigger_count,
        should_flush,
    )
    return should_flush


async def flush(conn: aiosqlite.Connection, app, intent_id: int) -> None:
    """Drain event_pending for intent_id → call on_match.

    DELETE ... RETURNING is a single atomic statement on SQLite ≥ 3.35
    (guaranteed by the Dockerfile python:3.12 → SQLite 3.41.2 baseline).
    Commit happens before on_match is awaited. on_match failure is logged but
    NOT re-raised — the buffer is already cleared, same contract as the cron
    path.
    """
    from sembr.matcher.callback import Match  # noqa: PLC0415

    # Atomic read-and-delete: no SELECT/DELETE race window (🟡1 fix).
    async with conn.execute(
        "DELETE FROM event_pending WHERE intent_id=? RETURNING members_json",
        (intent_id,),
    ) as cur:
        rows = await cur.fetchall()

    # Always commit — a zero-row DELETE still opens an implicit transaction.
    await conn.commit()

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

    # Prefer on_match_event (event-mode path, persist=False) over the cron
    # on_match handler so event-mode results are not persisted to history.
    on_match = getattr(app.state, "on_match_event", None) or getattr(app.state, "on_match", None)
    if on_match is None:
        logger.debug("flush intent_id=%d: no on_match handler registered", intent_id)
        return

    try:
        await on_match(matches)
        logger.info("flush intent_id=%d: pushed %d matches", intent_id, len(matches))
    except Exception as exc:
        logger.warning(
            "flush intent_id=%d: on_match raised (buffer already cleared): %s",
            intent_id,
            exc,
            exc_info=True,
        )


async def sweep_timed_out(
    conn: aiosqlite.Connection,
    app,
    event_intent_cache: EventIntentCache,
) -> None:
    """Flush intents whose oldest buffered group has exceeded max_wait_seconds.

    Called every 30 s by the event_y_sweeper APScheduler job. Each intent
    failure is isolated — one bad flush does not abort others.
    """
    from sembr.models import EventSchedule  # noqa: PLC0415

    _default_max_wait: int = EventSchedule.model_fields["max_wait_seconds"].default

    now = datetime.now(UTC)

    async with conn.execute(
        "SELECT intent_id, MIN(created_at) AS earliest FROM event_pending GROUP BY intent_id"
    ) as cur:
        rows = await cur.fetchall()

    for intent_id, earliest_str in rows:
        entry = event_intent_cache.get(intent_id)
        if entry is None:
            logger.warning(
                "sweep: intent_id=%d has buffered events but no cache entry; flushing anyway",
                intent_id,
            )
            max_wait = _default_max_wait
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
                intent_id,
                age_seconds,
                max_wait,
            )
            try:
                await flush(conn, app, intent_id)
            except Exception as exc:
                logger.warning(
                    "sweep: flush intent_id=%d failed: %s", intent_id, exc, exc_info=True
                )

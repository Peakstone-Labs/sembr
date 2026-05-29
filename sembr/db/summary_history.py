# SPDX-License-Identifier: Apache-2.0
"""Summary history persistence.

Stores cron-mode LLM summary results in ``summary_history`` so subsequent
scans can inject past N days as ``{history}`` context into the prompt.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta

import aiosqlite

from sembr.db.sqlite import transaction
from sembr.summarizer.models import SummaryResult

logger = logging.getLogger(__name__)

_RUN_AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


async def init_summary_history_table(conn: aiosqlite.Connection) -> None:
    """Create summary_history table and index if they don't exist (idempotent)."""
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_history (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            intent_id   INTEGER  NOT NULL REFERENCES intents(id) ON DELETE CASCADE,
            run_at      TEXT     NOT NULL,
            summary     TEXT     NOT NULL,
            citations   TEXT     NOT NULL DEFAULT '[]'
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_summary_history_intent_run_at
            ON summary_history(intent_id, run_at DESC)
        """
    )
    await conn.commit()


async def migrate_summary_history_unique_index(conn: aiosqlite.Connection) -> None:
    """Add UNIQUE(intent_id, run_at) — backfill idempotency anchor.

    Runs inside a single transaction:
      1. Delete duplicate ``(intent_id, run_at)`` rows keeping the lowest ``id``.
         Uses a correlated-subquery DELETE that's portable to SQLite 3.15+
         (every supported aiosqlite shim).
      2. ``CREATE UNIQUE INDEX IF NOT EXISTS`` — idempotent on re-run.

    Must be called after ``init_summary_history_table``.  Safe to invoke on
    every startup; the IF NOT EXISTS guard short-circuits when the index is
    already present.
    """
    async with transaction() as txn:
        await txn.execute(
            """
            DELETE FROM summary_history
            WHERE id IN (
                SELECT id FROM summary_history AS outer_t
                WHERE EXISTS (
                    SELECT 1 FROM summary_history AS inner_t
                    WHERE inner_t.intent_id = outer_t.intent_id
                      AND inner_t.run_at = outer_t.run_at
                      AND inner_t.id < outer_t.id
                )
            )
            """
        )
        await txn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uniq_summary_history_intent_run_at
                ON summary_history(intent_id, run_at)
            """
        )


async def save_summary(
    conn: aiosqlite.Connection,
    result: SummaryResult,
    run_at: str | None = None,
) -> int:
    """Persist a SummaryResult to summary_history; returns the inserted row id.

    ``conn`` is accepted for test-compatibility: ``install_for_test`` installs
    the test connection as the singleton used by ``transaction()``.  Production
    callers should pass ``get_conn()``; the actual write goes through
    ``transaction()`` which acquires ``_WRITE_LOCK`` on the global singleton.

    Plain ``INSERT``: if the new UNIQUE(intent_id, run_at) index trips, the
    caller sees ``sqlite3.IntegrityError`` — backfill takes the
    :func:`save_summary_or_skip` variant instead.
    """
    if run_at is None:
        run_at = datetime.now(UTC).strftime(_RUN_AT_FORMAT)
    citations_json = json.dumps([dataclasses.asdict(c) for c in result.citations])
    row_id: int | None = None
    async with transaction() as txn:
        async with txn.execute(
            "INSERT INTO summary_history (intent_id, run_at, summary, citations) VALUES (?, ?, ?, ?)",
            (result.intent_id, run_at, result.summary, citations_json),
        ) as cur:
            row_id = cur.lastrowid
    assert row_id is not None, "INSERT must yield lastrowid"
    return row_id


async def save_summary_or_skip(
    conn: aiosqlite.Connection,
    result: SummaryResult,
    run_at: str,
) -> bool:
    """Backfill-friendly variant: ``INSERT OR IGNORE`` returning whether the row was new.

    Returns ``True`` when the row was inserted, ``False`` when UNIQUE(intent_id,
    run_at) silently dropped it (a normal cron tick or a prior backfill already
    wrote that fire-time).  Used by backfill so re-running the same N-window is
    idempotent.
    """
    citations_json = json.dumps([dataclasses.asdict(c) for c in result.citations])
    inserted = False
    async with transaction() as txn:
        async with txn.execute(
            "INSERT OR IGNORE INTO summary_history (intent_id, run_at, summary, citations) VALUES (?, ?, ?, ?)",
            (result.intent_id, run_at, result.summary, citations_json),
        ) as cur:
            inserted = cur.rowcount == 1
    return inserted


async def list_summaries(
    conn: aiosqlite.Connection,
    intent_id: int,
    limit: int = 100,
    offset: int = 0,
) -> list[dict]:
    """Return summary_history rows for the dashboard's History view.

    Order is ``run_at DESC`` so the newest run is at the top.  Returns a list
    of dicts with parsed citations; an empty list means no rows for the
    intent (or the intent itself does not exist — callers handle that).
    """
    rows: list[dict] = []
    async with conn.execute(
        """
        SELECT id, intent_id, run_at, summary, citations
        FROM summary_history
        WHERE intent_id = ?
        ORDER BY run_at DESC
        LIMIT ? OFFSET ?
        """,
        (intent_id, limit, offset),
    ) as cur:
        async for row in cur:
            try:
                citations = json.loads(row[4])
            except (TypeError, ValueError):
                citations = []
            rows.append(
                {
                    "id": row[0],
                    "intent_id": row[1],
                    "run_at": row[2],
                    "summary": row[3],
                    "citations": citations,
                }
            )
    return rows


async def list_summaries_between(
    conn: aiosqlite.Connection,
    intent_id: int,
    since_utc_iso: str,
    until_utc_iso: str,
) -> list[dict]:
    """Return summary_history rows in ``[since_utc_iso, until_utc_iso]`` inclusive.

    Rows are ordered ``run_at DESC`` (newest first).  Returns the same dict
    shape as :func:`list_summaries` so export / aggregate callers get a
    consistent schema.  An empty list means no rows for the intent in the range
    (or the intent itself does not exist — callers handle that).
    """
    rows: list[dict] = []
    async with conn.execute(
        """
        SELECT id, intent_id, run_at, summary, citations
        FROM summary_history
        WHERE intent_id = ?
          AND run_at >= ?
          AND run_at <= ?
        ORDER BY run_at DESC
        """,
        (intent_id, since_utc_iso, until_utc_iso),
    ) as cur:
        async for row in cur:
            try:
                citations = json.loads(row[4])
            except (TypeError, ValueError):
                citations = []
            rows.append(
                {
                    "id": row[0],
                    "intent_id": row[1],
                    "run_at": row[2],
                    "summary": row[3],
                    "citations": citations,
                }
            )
    return rows


async def delete_summary(
    conn: aiosqlite.Connection,
    intent_id: int,
    row_id: int,
) -> bool:
    """Delete one summary_history row and the match_seen rows for its citations.

    Returns ``True`` when a row was deleted; ``False`` when the row didn't
    exist or didn't belong to ``intent_id`` (API layer translates the False
    case to 404).  Scoping by ``intent_id`` prevents cross-intent deletes from
    a forged ``row_id``.

    Cascades into ``match_seen`` for each citation's ``article_id`` —
    "delete this summary row" is the user's way of saying "I don't want this
    history; let cron / backfill match these articles again."  Without this
    cascade, the article stays in match_seen forever and any future scan
    silently skips it.  Single transaction so a citations-cascade failure
    rolls the row delete back too.
    """
    async with transaction() as txn:
        # Read citations first so we know which article_ids to evict from
        # match_seen.  Scope by intent_id same as the DELETE for safety.
        async with txn.execute(
            "SELECT citations FROM summary_history WHERE id = ? AND intent_id = ?",
            (row_id, intent_id),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return False
        try:
            citations = json.loads(row[0]) if row[0] else []
        except (TypeError, ValueError):
            citations = []
        article_ids = [
            c.get("article_id") for c in citations if isinstance(c, dict) and c.get("article_id")
        ]

        if article_ids:
            placeholders = ",".join("?" for _ in article_ids)
            await txn.execute(
                f"DELETE FROM match_seen WHERE intent_id = ? AND article_id IN ({placeholders})",
                (intent_id, *article_ids),
            )

        async with txn.execute(
            "DELETE FROM summary_history WHERE id = ? AND intent_id = ?",
            (row_id, intent_id),
        ) as cur:
            return cur.rowcount == 1


async def format_history_text(
    conn: aiosqlite.Connection,
    intent_id: int,
    history_days: int,
    now: datetime | None = None,
) -> str:
    """Return past summaries as ``=== YYYY-MM-DD ===\\n<summary>`` blocks, DESC order.

    Caller guarantees ``history_days >= 1`` (Pydantic ge=1 enforces this at the
    API layer).  A guard is kept here to protect direct internal callers.

    ``now`` overrides the time anchor — used by backfill replays so the
    ``{history}`` slot for a past fire-time only sees summaries that existed
    at *that* moment, not present-time rows.  When omitted, ``now`` defaults
    to ``datetime.now(timezone.utc)`` (normal cron behaviour).

    The SQL clamps both ends: ``run_at >= effective_now - history_days`` and
    ``run_at <= effective_now``.  The upper bound matters during backfill —
    without it, a real cron row written *after* the simulated past fire-time
    would leak into the replay's prompt and break the oldest→newest causal
    chain.
    """
    if history_days < 1:
        return ""
    effective_now = now if now is not None else datetime.now(UTC)
    anchor_dt = effective_now - timedelta(days=history_days)
    anchor_ts = anchor_dt.strftime(_RUN_AT_FORMAT)
    upper_ts = effective_now.strftime(_RUN_AT_FORMAT)
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        """
        SELECT run_at, summary FROM summary_history
        WHERE intent_id = ?
          AND run_at >= ?
          AND run_at <= ?
        ORDER BY run_at DESC
        """,
        (intent_id, anchor_ts, upper_ts),
    ) as cur:
        async for row in cur:
            rows.append((row[0], row[1]))
    if not rows:
        return ""
    parts: list[str] = []
    for run_at, summary in rows:
        date_str = run_at[:10]  # "YYYY-MM-DD" from ISO-8601
        parts.append(f"=== {date_str} ===\n{summary}")
    return "\n\n".join(parts)

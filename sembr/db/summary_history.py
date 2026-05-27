# SPDX-License-Identifier: Apache-2.0
"""Summary history persistence.

Stores cron-mode LLM summary results in ``summary_history`` so subsequent
scans can inject past N days as ``{history}`` context into the prompt.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import datetime, timezone

import aiosqlite

from sembr.db.sqlite import transaction
from sembr.summarizer.models import SummaryResult

logger = logging.getLogger(__name__)


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
    """
    if run_at is None:
        run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


async def format_history_text(
    conn: aiosqlite.Connection,
    intent_id: int,
    history_days: int,
) -> str:
    """Return past summaries as ``=== YYYY-MM-DD ===\\n<summary>`` blocks, DESC order.

    Caller guarantees ``history_days >= 1`` (Pydantic ge=1 enforces this at the
    API layer).  A guard is kept here to protect direct internal callers.
    """
    if history_days < 1:
        return ""
    rows: list[tuple[str, str]] = []
    async with conn.execute(
        """
        SELECT run_at, summary FROM summary_history
        WHERE intent_id = ?
          AND run_at >= datetime('now', '-' || ? || ' days')
        ORDER BY run_at DESC
        """,
        (intent_id, history_days),
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

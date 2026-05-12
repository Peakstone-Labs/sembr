"""Event log tables for the monitoring dashboard (D1 / D2 / D3).

Two append-only tables:
  - feed_fetch_log : one row per collect_feed exit (success or failure)
  - embed_call_log : one row per embedder_worker batch outcome

Writes use independent transactions so they never share a BEGIN with the business
path that triggered them. Callers must wrap log_*_event in best-effort try/except
and only logger.warning on failure — never let observability faults kill business work.

Canonical `started_at` form: `datetime.isoformat()` on a tz-aware UTC datetime,
which produces `"YYYY-MM-DDTHH:MM:SS+00:00"`. Do NOT mix in `Z`-suffixed values
in the same column — `+00:00` and `Z` lex-sort differently and any range
comparison (retention cutoff, sparkline window) would silently break across
formats. The snapshot response field `generated_at` may use the `Z` shorthand
because it's response-only, never a query input.
"""

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite

from sembr.db.sqlite import transaction

logger = logging.getLogger(__name__)

_ERROR_MESSAGE_MAX = 500

_CREATE_FEED_FETCH_LOG = """
CREATE TABLE IF NOT EXISTS feed_fetch_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id         INTEGER NOT NULL REFERENCES feeds(id) ON DELETE CASCADE,
    started_at      TEXT NOT NULL,
    elapsed_ms      INTEGER NOT NULL,
    ok              INTEGER NOT NULL,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    items_new       INTEGER NOT NULL DEFAULT 0,
    error_class     TEXT,
    error_message   TEXT
)
"""

_CREATE_FEED_FETCH_IDX_FEED_STARTED = (
    "CREATE INDEX IF NOT EXISTS idx_feed_fetch_log_feed_started "
    "ON feed_fetch_log(feed_id, started_at DESC)"
)
_CREATE_FEED_FETCH_IDX_STARTED = (
    "CREATE INDEX IF NOT EXISTS idx_feed_fetch_log_started ON feed_fetch_log(started_at)"
)

_CREATE_EMBED_CALL_LOG = """
CREATE TABLE IF NOT EXISTS embed_call_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    elapsed_ms      INTEGER NOT NULL,
    ok              INTEGER NOT NULL,
    batch_size      INTEGER NOT NULL,
    total_chars     INTEGER NOT NULL,
    timeout_seconds REAL NOT NULL,
    error_class     TEXT,
    error_message   TEXT
)
"""

_CREATE_EMBED_CALL_IDX_STARTED = (
    "CREATE INDEX IF NOT EXISTS idx_embed_call_log_started ON embed_call_log(started_at)"
)


async def init_event_log_tables(conn: aiosqlite.Connection) -> None:
    """Idempotent DDL. Must run after init_feed_tables (FK references feeds.id)."""
    await conn.execute(_CREATE_FEED_FETCH_LOG)
    await conn.execute(_CREATE_FEED_FETCH_IDX_FEED_STARTED)
    await conn.execute(_CREATE_FEED_FETCH_IDX_STARTED)
    await conn.execute(_CREATE_EMBED_CALL_LOG)
    await conn.execute(_CREATE_EMBED_CALL_IDX_STARTED)
    await conn.commit()


def _truncate(msg: str | None) -> str | None:
    if msg is None:
        return None
    if len(msg) <= _ERROR_MESSAGE_MAX:
        return msg
    return msg[:_ERROR_MESSAGE_MAX]


async def log_fetch_event(
    *,
    feed_id: int,
    started_at: datetime,
    elapsed_ms: int,
    ok: bool,
    items_seen: int,
    items_new: int,
    error_class: str | None,
    error_message: str | None,
) -> None:
    """Insert one feed_fetch_log row in its own transaction.

    Caller must wrap in try/except — see D3.
    """
    async with transaction() as conn:
        await conn.execute(
            "INSERT INTO feed_fetch_log "
            "(feed_id, started_at, elapsed_ms, ok, items_seen, items_new, "
            " error_class, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                feed_id,
                started_at.isoformat(),
                int(elapsed_ms),
                1 if ok else 0,
                int(items_seen),
                int(items_new),
                error_class,
                _truncate(error_message),
            ),
        )


async def log_embed_event(
    *,
    started_at: datetime,
    elapsed_ms: int,
    ok: bool,
    batch_size: int,
    total_chars: int,
    timeout_seconds: float,
    error_class: str | None,
    error_message: str | None,
) -> None:
    """Insert one embed_call_log row in its own transaction."""
    async with transaction() as conn:
        await conn.execute(
            "INSERT INTO embed_call_log "
            "(started_at, elapsed_ms, ok, batch_size, total_chars, "
            " timeout_seconds, error_class, error_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                started_at.isoformat(),
                int(elapsed_ms),
                1 if ok else 0,
                int(batch_size),
                int(total_chars),
                float(timeout_seconds),
                error_class,
                _truncate(error_message),
            ),
        )

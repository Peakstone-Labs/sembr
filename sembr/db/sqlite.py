"""SQLite handle.

设计决策 #3 / #4: a single global aiosqlite connection per process. WAL must be on
before the first write — aiosqlite serialises writes internally, so a shared connection
is safe and avoids re-applying pragmas per request.
"""
from __future__ import annotations

import aiosqlite

_WAL_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

_conn: aiosqlite.Connection | None = None


async def init_sqlite(path: str) -> aiosqlite.Connection:
    """Open the global connection and apply WAL pragmas. Idempotent within a process."""
    global _conn
    if _conn is not None:
        return _conn
    conn = await aiosqlite.connect(path)
    for pragma in _WAL_PRAGMAS:
        await conn.execute(pragma)
    # journal_mode=WAL returns the resulting mode — verify it actually stuck.
    async with conn.execute("PRAGMA journal_mode") as cur:
        row = await cur.fetchone()
    mode = (row[0] if row else "").lower()
    if mode != "wal":
        await conn.close()
        raise RuntimeError(f"failed to enable WAL on {path!r}: journal_mode={mode!r}")
    await conn.commit()
    _conn = conn
    return conn


def get_conn() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("SQLite not initialised; call init_sqlite() first")
    return _conn


async def close_sqlite() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def sqlite_ok() -> bool:
    """Cheap liveness probe for /health: WAL still on + connection responsive."""
    if _conn is None:
        return False
    try:
        async with _conn.execute("PRAGMA journal_mode") as cur:
            row = await cur.fetchone()
        return bool(row) and row[0].lower() == "wal"
    except Exception:
        return False

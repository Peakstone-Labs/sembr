"""SQLite handle.

设计决策 #3 / #4: a single global aiosqlite connection per process. WAL must be on
before the first write — aiosqlite serialises writes internally, so a shared connection
is safe and avoids re-applying pragmas per request.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import aiosqlite

_WAL_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA cache_size=-64000",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)

_conn: aiosqlite.Connection | None = None
# Serialises all writers on the shared connection. Without this, two coroutines
# (e.g. concurrent collect_feed jobs) racing to issue BEGIN trigger SQLite's
# "cannot start a transaction within a transaction" error. SQLite WAL only
# admits one writer anyway, so the lock costs nothing in throughput.
_WRITE_LOCK: asyncio.Lock | None = None


async def init_sqlite(path: str) -> aiosqlite.Connection:
    """Open the global connection and apply WAL pragmas. Idempotent within a process."""
    global _conn, _WRITE_LOCK
    if _conn is not None:
        return _conn
    _WRITE_LOCK = asyncio.Lock()
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
    global _conn, _WRITE_LOCK
    if _conn is not None:
        await _conn.close()
        _conn = None
    _WRITE_LOCK = None


def install_for_test(conn: aiosqlite.Connection) -> None:
    """Register an externally-opened connection as the singleton + create the lock.

    Test-only. Skips the WAL pragma / verification so :memory: connections (which
    cannot use WAL) work. Production code must continue to call init_sqlite().
    """
    global _conn, _WRITE_LOCK
    _conn = conn
    _WRITE_LOCK = asyncio.Lock()


@asynccontextmanager
async def transaction():
    """Acquire the write lock and run a BEGIN…COMMIT block on the shared connection.

    Use for every multi-statement write path. Single-statement writes that follow
    `await conn.execute(...); await conn.commit()` should also wrap in this so they
    can't interleave with a multi-statement transaction in another coroutine.

    Self-heals on COMMIT/ROLLBACK failure: if the cleanup statement itself raises
    (e.g. SQLite reports "SQL statements in progress" because a concurrent SELECT
    cursor on the shared connection is mid-step), we still attempt to clear the
    transaction state with a best-effort ROLLBACK so the next caller's BEGIN
    isn't rejected with "cannot start a transaction within a transaction".
    Catches BaseException so an asyncio CancelledError mid-yield doesn't leak a
    half-open transaction either.
    """
    if _conn is None or _WRITE_LOCK is None:
        raise RuntimeError("SQLite not initialised; call init_sqlite() first")
    async with _WRITE_LOCK:
        await _conn.execute("BEGIN")
        try:
            yield _conn
        except BaseException:
            try:
                await _conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        else:
            try:
                await _conn.execute("COMMIT")
            except Exception:
                try:
                    await _conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise


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

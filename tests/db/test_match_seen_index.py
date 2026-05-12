# SPDX-License-Identifier: Apache-2.0
"""match_seen.article_id must have a single-column index, otherwise TTL-driven
`DELETE WHERE article_id IN (...)` falls back to a full scan and stalls the
global write lock.
"""

from __future__ import annotations

import aiosqlite
import pytest

from sembr.db.match_seen import init_match_seen_tables


@pytest.mark.asyncio
async def test_match_seen_article_id_index_exists():
    conn = await aiosqlite.connect(":memory:")
    try:
        # match_seen has a FK to intents but in-memory connection with foreign_keys
        # left at default (OFF) does not enforce FKs at DDL — index creation only
        # needs the column.
        await init_match_seen_tables(conn)
        async with conn.execute("PRAGMA index_list(match_seen)") as cur:
            rows = await cur.fetchall()
        names = {r[1] for r in rows}
        assert "idx_match_seen_article_id" in names

        # Also verify the index is on the right column.
        async with conn.execute("PRAGMA index_info(idx_match_seen_article_id)") as cur:
            cols = [r[2] for r in await cur.fetchall()]
        assert cols == ["article_id"]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_init_match_seen_tables_idempotent():
    conn = await aiosqlite.connect(":memory:")
    try:
        await init_match_seen_tables(conn)
        await init_match_seen_tables(conn)  # second call must not raise
    finally:
        await conn.close()

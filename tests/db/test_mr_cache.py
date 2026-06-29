# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.db.mr_cache — put/get/exists/count + override + FK cascade."""

from __future__ import annotations

import aiosqlite
import pytest

from sembr.db.intents import create_intent, delete_intent, init_intent_tables
from sembr.db.mr_cache import (
    extraction_exists,
    get_extraction,
    init_mr_cache_tables,
    put_extraction,
)
from sembr.db.sqlite import install_for_test
from sembr.models import IntentCreate

_AID = "11111111-1111-1111-1111-111111111111"
_VER = "deadbeefdeadbeef"
_EXTRACTION = {"source_org": "DB", "thesis": "t", "claims": [{"section": "x", "text": "y"}]}


def _intent() -> IntentCreate:
    return IntentCreate(
        name="mr", text="mr cache test", channels=[{"type": "email", "to": ["a@example.com"]}]
    )


async def _count_for(conn, intent_id: int, schema_version: str) -> int:
    """Local helper: count cached rows for (intent, schema_version)."""
    async with conn.execute(
        "SELECT COUNT(*) FROM mr_extraction_cache WHERE intent_id=? AND schema_version=?",
        (intent_id, schema_version),
    ) as cur:
        row = await cur.fetchone()
    return int(row[0]) if row else 0


@pytest.fixture
async def mem_conn():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


async def test_put_get_roundtrip(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    await put_extraction(
        mem_conn,
        article_id=_AID,
        intent_id=intent.id,
        schema_version=_VER,
        extraction=_EXTRACTION,
        title="Title",
        source_name="Feed A",
        published_at="2026-06-14T00:00:00Z",
    )
    got = await get_extraction(mem_conn, _AID, intent.id, _VER)
    assert got is not None
    assert got["extraction"] == _EXTRACTION  # JSON round-trips to the same dict
    assert got["title"] == "Title"
    assert got["source_name"] == "Feed A"
    assert got["published_at"] == "2026-06-14T00:00:00Z"
    assert got["created_at"]  # populated by DEFAULT


async def test_get_miss_returns_none(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version=_VER, extraction=_EXTRACTION
    )
    assert await get_extraction(mem_conn, _AID, intent.id, "otherversion00000") is None
    assert await get_extraction(mem_conn, "no-such-aid", intent.id, _VER) is None


async def test_exists(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    assert await extraction_exists(mem_conn, _AID, intent.id, _VER) is False
    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version=_VER, extraction=_EXTRACTION
    )
    assert await extraction_exists(mem_conn, _AID, intent.id, _VER) is True


async def test_override_replaces_and_refreshes_created_at(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    # Seed a row with an artificially old created_at to prove REPLACE re-defaults it.
    async with mem_conn.execute(
        """INSERT INTO mr_extraction_cache
               (article_id, intent_id, schema_version, extraction, created_at)
           VALUES (?,?,?,?, '2020-01-01T00:00:00')""",
        (_AID, intent.id, _VER, '{"old": true}'),
    ):
        pass
    await mem_conn.commit()

    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version=_VER, extraction=_EXTRACTION
    )
    got = await get_extraction(mem_conn, _AID, intent.id, _VER)
    assert got["extraction"] == _EXTRACTION  # overwritten, not appended
    assert got["created_at"] != "2020-01-01T00:00:00"  # DEFAULT re-fired on REPLACE
    assert await _count_for(mem_conn, intent.id, _VER) == 1  # still exactly one row


async def test_count_for_isolates_intent_and_version(mem_conn):
    a = await create_intent(mem_conn, _intent())
    b = await create_intent(mem_conn, _intent())
    await put_extraction(
        mem_conn, article_id="aid-1", intent_id=a.id, schema_version=_VER, extraction=_EXTRACTION
    )
    await put_extraction(
        mem_conn, article_id="aid-2", intent_id=a.id, schema_version=_VER, extraction=_EXTRACTION
    )
    await put_extraction(
        mem_conn, article_id="aid-1", intent_id=a.id, schema_version="v2", extraction=_EXTRACTION
    )
    await put_extraction(
        mem_conn, article_id="aid-1", intent_id=b.id, schema_version=_VER, extraction=_EXTRACTION
    )
    assert await _count_for(mem_conn, a.id, _VER) == 2  # two articles, this version
    assert await _count_for(mem_conn, a.id, "v2") == 1  # different version isolated
    assert await _count_for(mem_conn, b.id, _VER) == 1  # different intent isolated


async def test_same_article_two_versions_coexist(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version="v1", extraction={"a": 1}
    )
    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version="v2", extraction={"a": 2}
    )
    assert (await get_extraction(mem_conn, _AID, intent.id, "v1"))["extraction"] == {"a": 1}
    assert (await get_extraction(mem_conn, _AID, intent.id, "v2"))["extraction"] == {"a": 2}


async def test_delete_intent_cascades_cache(mem_conn):
    intent = await create_intent(mem_conn, _intent())
    await put_extraction(
        mem_conn, article_id=_AID, intent_id=intent.id, schema_version=_VER, extraction=_EXTRACTION
    )
    assert await _count_for(mem_conn, intent.id, _VER) == 1
    await delete_intent(mem_conn, intent.id)
    assert await _count_for(mem_conn, intent.id, _VER) == 0  # FK ON DELETE CASCADE

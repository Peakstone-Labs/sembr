# SPDX-License-Identifier: Apache-2.0
"""T2 (design §6): map_for_reduce — cache hit reuses (no LLM), miss extracts +
persists (D4), per-article failure / empty body degrade to an empty record without
sinking the run (D2), and index = 1-based recall order so [N] aligns with citations.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.mr_cache import get_extraction, init_mr_cache_tables, put_extraction
from sembr.db.sqlite import install_for_test
from sembr.matcher.callback import Match
from sembr.models import IntentCreate
from sembr.summarizer.mr_extract import map_for_reduce
from sembr.summarizer.spec import GeneratedSpec, compile_validator

_VER = "v1schema00000000"
_SPEC = GeneratedSpec(
    name="intent-1",
    extraction_prompt="extract facts",
    sections=[{"key": "facts", "label": "事实"}],
    schema_version=_VER,
)
_VALIDATOR = compile_validator(_SPEC)


@pytest.fixture
async def mem_conn():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


def _match(aid: str, *, title: str = "T", body: str = "Body text") -> Match:
    return Match(
        intent_id=1,
        article_id=aid,
        score=0.9,
        payload={
            "title": title,
            "body": body,
            "url": "https://example.com",
            "feed_id": 1,
            "published_at": "2026-06-01T00:00:00Z",
        },
    )


def _good_structured() -> AsyncMock:
    async def fake(prompt, schema, *, system=None, model=None, repair_attempts=2):
        if "FAILME" in prompt:
            raise RuntimeError("provider boom")
        return schema(
            no_relevant_content=False,
            source_org="Org",
            thesis="t",
            claims=[{"section": "facts", "text": "fact text", "quote": "q"}],
        )

    return AsyncMock(side_effect=fake)


async def _run(matches, llm, intent_id, *, concurrency=4):
    return await map_for_reduce(
        matches,
        intent_id=intent_id,
        intent_text="topic",
        spec=_SPEC,
        validator=_VALIDATOR,
        schema_version=_VER,
        llm=llm,
        model="m",
        concurrency=concurrency,
        feed_name_map={1: "Feed A"},
    )


async def test_miss_extracts_and_persists(mem_conn):
    intent = await create_intent(
        mem_conn, IntentCreate(name="i", text="t", channels=[{"type": "email", "to": ["a@b.com"]}])
    )
    llm = MagicMock()
    llm.structured = _good_structured()
    aid = "11111111-1111-1111-1111-111111111111"

    records, n_failed = await _run([_match(aid)], llm, intent.id)

    assert n_failed == 0
    assert llm.structured.await_count == 1  # miss → one extract call
    assert records[0]["index"] == 1
    assert records[0]["claims"][0]["text"] == "fact text"
    # persisted to cache (D4) so a second recall hits
    assert await get_extraction(mem_conn, aid, intent.id, _VER) is not None


async def test_cache_hit_reuses_without_llm(mem_conn):
    intent = await create_intent(
        mem_conn, IntentCreate(name="i", text="t", channels=[{"type": "email", "to": ["a@b.com"]}])
    )
    a1 = "11111111-1111-1111-1111-111111111111"
    a2 = "22222222-2222-2222-2222-222222222222"
    await put_extraction(
        mem_conn,
        article_id=a1,
        intent_id=intent.id,
        schema_version=_VER,
        extraction={
            "source_org": "Cached",
            "thesis": "c",
            "claims": [{"section": "facts", "text": "cached fact"}],
        },
        published_at="2026-05-30T00:00:00Z",
    )
    llm = MagicMock()
    llm.structured = _good_structured()

    records, n_failed = await _run([_match(a1), _match(a2)], llm, intent.id)

    assert n_failed == 0
    assert llm.structured.await_count == 1  # only a2 (the miss) hit the LLM
    # order preserved: index 1 = cached, index 2 = freshly mapped
    assert records[0]["index"] == 1
    assert records[0]["claims"][0]["text"] == "cached fact"
    assert records[0]["source_name"] == "Feed A"  # attached at runtime
    assert records[1]["index"] == 2
    assert records[1]["claims"][0]["text"] == "fact text"


async def test_per_article_failure_isolated(mem_conn):
    intent = await create_intent(
        mem_conn, IntentCreate(name="i", text="t", channels=[{"type": "email", "to": ["a@b.com"]}])
    )
    llm = MagicMock()
    llm.structured = _good_structured()
    good = _match("11111111-1111-1111-1111-111111111111", title="ok")
    bad = _match("22222222-2222-2222-2222-222222222222", title="FAILME")

    records, n_failed = await _run([good, bad], llm, intent.id)

    assert n_failed == 1  # one article failed, the other survived
    assert records[0]["claims"][0]["text"] == "fact text"
    assert records[1].get("no_relevant_content") is True
    assert records[1]["claims"] == []
    assert records[1]["index"] == 2  # failed record keeps its index for [N] alignment


async def test_empty_body_skips_llm(mem_conn):
    intent = await create_intent(
        mem_conn, IntentCreate(name="i", text="t", channels=[{"type": "email", "to": ["a@b.com"]}])
    )
    llm = MagicMock()
    llm.structured = _good_structured()

    records, n_failed = await _run(
        [_match("33333333-3333-3333-3333-333333333333", body="   ")], llm, intent.id
    )

    assert n_failed == 1
    assert llm.structured.await_count == 0  # empty body never calls the LLM
    assert records[0].get("no_relevant_content") is True

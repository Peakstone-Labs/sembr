# SPDX-License-Identifier: Apache-2.0
"""T3/T4/T10 (design §6): compute_summary facts branch.

- T4: extraction_enabled=False → {articles} slot is byte-identical to
  _build_articles_text (keep-path zero behaviour change); reduce_mode="raw".
- T3: extraction_enabled=True → facts (PREAMBLE_V2 + [N]) fill {articles}, raw
  body format is gone; reduce_mode="facts".
- T10: reduce_mode covers raw / facts / facts_partial / facts_fallback_raw.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.mr_cache import init_mr_cache_tables
from sembr.db.sqlite import install_for_test
from sembr.matcher.callback import Match
from sembr.models import IntentCreate
from sembr.summarizer.facts_render import PREAMBLE_V2
from sembr.summarizer.pipeline import SummaryPipeline, _build_articles_text


def _match(aid: str = "a1", *, title: str = "Test title", body: str = "Body text") -> Match:
    return Match(
        intent_id=1,
        article_id=aid,
        score=0.85,
        payload={
            "title": title,
            "body": body,
            "url": "https://example.com",
            "feed_id": 1,
            "published_at": "2026-05-01T00:00:00Z",
        },
    )


def _write_base_templates(d: Path) -> None:
    (d / "system").mkdir()
    (d / "instruction").mkdir()
    (d / "system" / "default.md").write_text("Assistant. Language: {language}", encoding="utf-8")
    (d / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )


def _write_spec(d: Path, name: str = "intent-1") -> None:
    ext = d / "extraction"
    ext.mkdir(exist_ok=True)
    (ext / f"{name}.md").write_text("Extract structured facts.", encoding="utf-8")
    (ext / f"{name}.json").write_text(
        json.dumps(
            {
                "sections": [{"key": "facts", "label": "事实"}],
                "article_fields": [],
                "common_claim_fields": [],
            }
        ),
        encoding="utf-8",
    )


def _facts_llm(summary: str = "digest") -> MagicMock:
    """Fake backend: summarize() for reduce, structured() for map."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value=summary)
    llm.max_prompt_chars = 2_000_000

    async def structured(prompt, schema, *, system=None, model=None, repair_attempts=2):
        if "FAILME" in prompt:
            raise RuntimeError("provider boom")
        return schema(
            no_relevant_content=False,
            source_org="Fed",
            thesis="thesis text",
            claims=[{"section": "facts", "text": "held rates", "quote": "held at 5.25%"}],
        )

    llm.structured = AsyncMock(side_effect=structured)
    return llm


def _ctx(extraction_enabled: bool):
    async def ctx(iid):
        return "default", "default", "AI news", "zh", None, extraction_enabled

    return ctx


def _prompt_of(llm: MagicMock) -> str:
    call = llm.summarize.call_args
    return call[0][0] if call[0] else call[1]["prompt"]


@pytest.fixture
async def env(tmp_path):
    """Installed db + an intent(id=1) + a prompts_dir with base templates + spec."""
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    intent = await create_intent(
        conn,
        IntentCreate(name="i", text="AI news", channels=[{"type": "email", "to": ["a@b.com"]}]),
    )
    assert intent.id == 1  # matches _match().intent_id
    _write_base_templates(tmp_path)
    _write_spec(tmp_path)
    yield tmp_path
    await conn.close()


def _pipeline(prompts_dir: Path, llm, *, extraction_enabled: bool) -> SummaryPipeline:
    return SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx(extraction_enabled),
        prompts_dir=prompts_dir,
        get_reduce_ctx=lambda: ("m", 4),
    )


# --- T4: keep-path byte-identical -----------------------------------------


async def test_keep_path_articles_byte_identical(env):
    """extraction_enabled=False → {articles} slot == _build_articles_text output."""
    llm = _facts_llm()
    pipeline = _pipeline(env, llm, extraction_enabled=False)
    match = _match()

    result = await pipeline.compute_summary([match])

    assert result is not None
    assert result.reduce_mode == "raw"
    llm.structured.assert_not_called()  # never enters the map path
    expected_articles, _, _ = _build_articles_text([match], 10_000_000)
    assert _prompt_of(llm) == f"Topic: AI news\n\n{expected_articles}"


# --- T3: facts branch ------------------------------------------------------


async def test_facts_branch_injects_facts(env):
    """extraction_enabled=True → facts (preamble + [N]) fill {articles}."""
    llm = _facts_llm()
    pipeline = _pipeline(env, llm, extraction_enabled=True)

    result = await pipeline.compute_summary([_match()])

    assert result is not None
    assert result.reduce_mode == "facts"
    llm.structured.assert_awaited()  # map ran
    prompt = _prompt_of(llm)
    assert PREAMBLE_V2 in prompt
    assert "held rates" in prompt
    assert '〔原文: "held at 5.25%"〕' in prompt
    # raw body format must be gone (no "Source: <url>" entry block)
    assert "Source: https://example.com" not in prompt


async def test_facts_n_aligns_with_citations(env):
    """[N] in facts ≤ len(citations); citation order is recall order (T6 smoke)."""
    llm = _facts_llm()
    pipeline = _pipeline(env, llm, extraction_enabled=True)
    a, b = _match("a1"), _match("a2")

    result = await pipeline.compute_summary([a, b])

    assert result is not None
    assert len(result.citations) == 2
    prompt = _prompt_of(llm)
    assert "[1] " in prompt and "[2] " in prompt
    assert "[3] " not in prompt  # never references beyond the citation set


# --- T10: reduce_mode four states -----------------------------------------


async def test_reduce_mode_raw(env):
    llm = _facts_llm()
    result = await _pipeline(env, llm, extraction_enabled=False).compute_summary([_match()])
    assert result.reduce_mode == "raw"


async def test_reduce_mode_facts(env):
    llm = _facts_llm()
    result = await _pipeline(env, llm, extraction_enabled=True).compute_summary([_match()])
    assert result.reduce_mode == "facts"


async def test_reduce_mode_facts_partial(env):
    """One article fails to map (FAILME) but another yields facts → facts_partial."""
    llm = _facts_llm()
    pipeline = _pipeline(env, llm, extraction_enabled=True)
    result = await pipeline.compute_summary(
        [_match("a1", title="ok"), _match("a2", title="FAILME")]
    )
    assert result is not None
    assert result.reduce_mode == "facts_partial"


async def test_reduce_mode_fallback_raw_when_spec_missing(tmp_path):
    """extraction on but no spec file → SpecNotFoundError → fail-open to raw."""
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    await create_intent(
        conn,
        IntentCreate(name="i", text="AI news", channels=[{"type": "email", "to": ["a@b.com"]}]),
    )
    _write_base_templates(tmp_path)  # NOTE: no _write_spec → spec missing
    try:
        llm = _facts_llm()
        pipeline = _pipeline(tmp_path, llm, extraction_enabled=True)
        result = await pipeline.compute_summary([_match()])
        assert result is not None
        assert result.reduce_mode == "facts_fallback_raw"
        # fell back to raw articles
        assert "Source: https://example.com" in _prompt_of(llm)
    finally:
        await conn.close()


async def test_reduce_mode_fallback_raw_when_all_fail(env):
    """extraction on, every article fails to map → no facts → fail-open to raw."""
    llm = _facts_llm()
    pipeline = _pipeline(env, llm, extraction_enabled=True)
    result = await pipeline.compute_summary([_match("a1", title="FAILME")])
    assert result is not None
    assert result.reduce_mode == "facts_fallback_raw"
    assert "Source: https://example.com" in _prompt_of(llm)


async def test_facts_over_budget_dequote_then_fallback_raw(env):
    """§4.4: facts map OK but overflow budget → dequote → still over → fallback_raw.

    PREAMBLE_V2 alone is >800 chars, so a ~1000-char prompt budget can't fit facts
    even after dropping quotes, while the tiny raw body still fits."""
    llm = _facts_llm()
    llm.max_prompt_chars = 1000
    pipeline = _pipeline(env, llm, extraction_enabled=True)

    result = await pipeline.compute_summary([_match()])

    assert result is not None
    assert result.reduce_mode == "facts_fallback_raw"
    llm.structured.assert_awaited()  # map DID run (facts built) — not a spec/all-fail case
    assert "Source: https://example.com" in _prompt_of(llm)  # raw fallback used

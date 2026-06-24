# SPDX-License-Identifier: Apache-2.0
"""QA tests: T5 (fail-open degradation) and T6 ([N]<->citations alignment).

T5 — beyond the dev-owned smoke tests in test_pipeline_facts.py:
  - T5a: ALL articles have no_relevant_content (zero claims) → fall-open to raw,
         result is non-empty, reduce_mode="facts_fallback_raw".
  - T5b: spec missing → fallback raw, result non-empty, reduce_mode="facts_fallback_raw".
  - T5c: partial failure (one OK, one fails) → only OK facts used, result non-empty,
         NOT a fallback, reduce_mode="facts_partial".
  - T5d: partial failure does NOT mix the raw body of the failed article into the
         facts prompt. Only the OK article's facts appear.
  - T5e: all_fail variant — every article body is empty → zero facts → fail-open,
         raw fallback is non-empty (can still build a raw digest), no crash.

T6 — [N]<->citations alignment, extending existing T6 smoke:
  - T6a: N articles → every [N] reference (1-based) in the facts prompt is within
         [1..len(citations)], no out-of-range [N+1] or [0].
  - T6b: a no_relevant_content article still occupies its [N] position in the
         article list and in citations; it does not shift other articles' [N].
  - T6c: citations order mirrors the ordered (published_at desc) order; index N in
         facts corresponds to citations[N-1].
  - T6d: citations[N-1].article_id matches the article whose index is N in the
         facts text, for each N (1..len).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.mr_cache import init_mr_cache_tables
from sembr.db.sqlite import install_for_test
from sembr.matcher.callback import Match
from sembr.models import IntentCreate
from sembr.summarizer.pipeline import SummaryPipeline

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _match(
    aid: str,
    *,
    title: str = "Test article",
    body: str = "Long enough body text for a real article.",
    published_at: str = "2026-06-10T00:00:00Z",
) -> Match:
    return Match(
        intent_id=1,
        article_id=aid,
        score=0.85,
        payload={
            "title": title,
            "body": body,
            "url": f"https://example.com/{aid}",
            "feed_id": 1,
            "published_at": published_at,
        },
    )


def _write_base_templates(d: Path) -> None:
    (d / "system").mkdir(exist_ok=True)
    (d / "instruction").mkdir(exist_ok=True)
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


def _ctx(extraction_enabled: bool = True):
    async def ctx(iid):
        return "default", "default", "AI news", "zh", None, extraction_enabled

    return ctx


def _make_llm(
    *,
    summarize_return: str = "digest text",
    fail_if: str | None = None,
) -> MagicMock:
    """LLM fake: structured() succeeds unless title contains fail_if."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value=summarize_return)
    llm.max_prompt_chars = 2_000_000

    async def structured(prompt, schema, *, system=None, model=None, repair_attempts=2):
        if fail_if and fail_if in prompt:
            raise RuntimeError(f"structured: forced failure for '{fail_if}'")
        return schema(
            no_relevant_content=False,
            source_org="TestOrg",
            thesis="thesis text",
            claims=[{"section": "facts", "text": "some fact", "quote": "verbatim"}],
        )

    llm.structured = AsyncMock(side_effect=structured)
    return llm


def _make_pipeline(prompts_dir: Path, llm) -> SummaryPipeline:
    return SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx(extraction_enabled=True),
        prompts_dir=prompts_dir,
        get_reduce_ctx=lambda: ("test-model", 4),
    )


def _prompt_of(llm: MagicMock) -> str:
    call = llm.summarize.call_args
    return call[0][0] if call[0] else call[1]["prompt"]


def _refs_in_prompt(prompt: str) -> set[int]:
    """Return the set of all [N] integer references found in the facts prompt."""
    return {int(m) for m in re.findall(r"\[(\d+)\]", prompt)}


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def env(tmp_path):
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    intent = await create_intent(
        conn,
        IntentCreate(name="i", text="AI news", channels=[{"type": "email", "to": ["a@b.com"]}]),
    )
    assert intent.id == 1
    _write_base_templates(tmp_path)
    _write_spec(tmp_path)
    yield tmp_path
    await conn.close()


# ===========================================================================
# T5 — fail-open degradation: product usability assertions
# ===========================================================================


async def test_t5a_all_no_relevant_content_falls_open(env):
    """T5a: all articles map to no_relevant_content (zero claims) → zero facts
    → D2 fail-open → raw fallback → result is non-empty and usable.

    This covers the path: map succeeds but no facts were extracted (all articles
    had no_relevant_content=True). The pipeline must NOT return an empty/error
    digest; it must fall back and produce a raw-body digest.
    """
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="raw fallback digest")
    llm.max_prompt_chars = 2_000_000

    async def structured_no_relevant(prompt, schema, *, system=None, model=None, repair_attempts=2):
        return schema(
            no_relevant_content=True,
            source_org=None,
            thesis=None,
            claims=[],
        )

    llm.structured = AsyncMock(side_effect=structured_no_relevant)
    pipeline = _make_pipeline(env, llm)

    result = await pipeline.compute_summary(
        [
            _match("a1"),
            _match("a2"),
        ]
    )

    # Must produce a usable digest (not None, not empty summary)
    assert result is not None, "D2 fail-open must still produce a digest"
    assert result.summary, "summary must be non-empty after fail-open"
    assert result.reduce_mode == "facts_fallback_raw"
    # Fallen back to raw body path → raw article body format in prompt
    prompt = _prompt_of(llm)
    assert "Source: https://example.com" in prompt, (
        "raw path must use Source: <url> format; got: " + prompt[:200]
    )


async def test_t5b_spec_missing_fallback_non_empty(tmp_path):
    """T5b: extraction_enabled but spec file absent → SpecNotFoundError →
    fail-open to raw bodies → result is non-empty.

    Verifies the fail-open is not just a mode label but an actual usable digest.
    """
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    await create_intent(
        conn,
        IntentCreate(name="i", text="AI news", channels=[{"type": "email", "to": ["a@b.com"]}]),
    )
    _write_base_templates(tmp_path)
    # No spec written → SpecNotFoundError path
    try:
        llm = _make_llm(summarize_return="spec-missing fallback digest")
        pipeline = _make_pipeline(tmp_path, llm)

        result = await pipeline.compute_summary([_match("a1")])

        assert result is not None
        assert result.summary == "spec-missing fallback digest"
        assert result.reduce_mode == "facts_fallback_raw"
        # structured was never called (failed before map)
        llm.structured.assert_not_called()
        # Raw body used
        assert "Source: https://example.com" in _prompt_of(llm)
    finally:
        await conn.close()


async def test_t5c_partial_failure_uses_ok_facts_not_fallback(env):
    """T5c: one article fails to map, one succeeds → facts from the OK article
    used, no fallback (reduce_mode="facts_partial"), result non-empty.

    Key distinction from full-failure: we must NOT fall back to raw. The
    successful article's facts are enough to proceed.
    """
    llm = _make_llm(fail_if="FAILME")
    pipeline = _make_pipeline(env, llm)

    result = await pipeline.compute_summary(
        [
            _match("a1", title="ok"),
            _match("a2", title="FAILME"),
        ]
    )

    assert result is not None
    assert result.summary, "partial failure still produces a non-empty summary"
    assert result.reduce_mode == "facts_partial", (
        f"partial map failure must be facts_partial, got {result.reduce_mode}"
    )
    prompt = _prompt_of(llm)
    # Facts path used (PREAMBLE_V2 present) — NOT the raw path
    from sembr.summarizer.facts_render import PREAMBLE_V2

    assert PREAMBLE_V2 in prompt, "facts path must be used for partial failure"
    # Raw body format must NOT appear (not a fallback)
    assert "Source: https://example.com/a1" not in prompt, (
        "raw article body format must not appear in partial-facts path"
    )


async def test_t5d_partial_failure_failed_article_body_not_in_facts(env):
    """T5d: the raw body of the FAILED article (a2) must NOT be mixed into the
    facts prompt. Only a2's [N] position appears in the article list (no_relevant_
    content), but none of its raw body text leaks into the facts section.
    """
    llm = _make_llm(fail_if="FAILME")
    pipeline = _make_pipeline(env, llm)
    failed_body = "Long enough body text for a real article."  # default body

    result = await pipeline.compute_summary(
        [
            _match("a1", title="ok"),
            _match("a2", title="FAILME", body=failed_body),
        ]
    )

    assert result is not None
    assert result.reduce_mode == "facts_partial"
    prompt = _prompt_of(llm)
    # The failed article's raw body must NOT appear in the facts prompt
    assert failed_body not in prompt, (
        "Failed article's raw body text must not be mixed into the facts prompt"
    )


async def test_t5e_all_empty_body_fallopen_produces_digest(env):
    """T5e: all articles have empty bodies → map skips LLM → all no_relevant_content
    → zero facts → D2 fail-open → raw bodies path → but raw bodies are also empty
    so _build_articles_text still produces something (even if minimal).

    Crucial: the pipeline must not crash or return None due to this path.
    """
    llm = _make_llm()
    pipeline = _make_pipeline(env, llm)

    result = await pipeline.compute_summary(
        [
            _match("a1", body="   "),
            _match("a2", body=""),
        ]
    )

    assert result is not None, "all-empty-body fallback must not return None"
    assert result.reduce_mode == "facts_fallback_raw"
    # structured never called (empty body skips the LLM map)
    llm.structured.assert_not_called()


# ===========================================================================
# T6 — [N] <-> citations alignment
# ===========================================================================


async def test_t6a_all_refs_within_citation_bounds(env):
    """T6a: every [N] reference in the facts prompt (from article list, thesis,
    claims) is within [1..len(citations)]. No [0] or out-of-range reference.

    Uses 3 articles → citations = 3; any [4] would be an invalid reference.
    """
    llm = _make_llm()
    pipeline = _make_pipeline(env, llm)
    # 3 articles with same timestamp so only article_id tiebreak applies
    matches = [
        _match("a1", published_at="2026-06-10T00:00:00Z"),
        _match("a2", published_at="2026-06-09T00:00:00Z"),
        _match("a3", published_at="2026-06-08T00:00:00Z"),
    ]

    result = await pipeline.compute_summary(matches)

    assert result is not None
    assert len(result.citations) == 3
    prompt = _prompt_of(llm)
    refs = _refs_in_prompt(prompt)
    assert refs, "at least some [N] refs must appear in facts"
    assert refs.issubset({1, 2, 3}), f"All [N] refs must be within [1..3], found {refs}"
    assert 0 not in refs, "No [0] reference allowed"


async def test_t6b_failed_article_preserves_index_alignment(env):
    """T6b: a no_relevant_content article holds its [N] position in the article
    list and in citations, without shifting other articles' indices.

    Article a2 fails (title=FAILME) → n_failed=1 → facts_partial. Its position
    [2] in the article list must still exist, and citations[1].article_id must
    be a2, not a3 shifted up.
    """
    llm = _make_llm(fail_if="FAILME")
    pipeline = _make_pipeline(env, llm)
    # 3 articles: a1 (newest), a2 (fails, middle), a3 (oldest)
    matches = [
        _match("a1", published_at="2026-06-10T00:00:00Z"),
        _match("a2", title="FAILME", published_at="2026-06-09T00:00:00Z"),
        _match("a3", published_at="2026-06-08T00:00:00Z"),
    ]

    result = await pipeline.compute_summary(matches)

    assert result is not None
    assert result.reduce_mode == "facts_partial"
    assert len(result.citations) == 3, "all 3 articles must be in citations"

    prompt = _prompt_of(llm)
    # [2] must appear in the article list even though a2 failed
    assert "[2]" in prompt, "Failed article must still appear as [2] in article list"

    # citations[1] = a2 (index 2, 0-based = 1)
    # ordered = sorted by published_at desc → a1, a2, a3
    # citations[0]=a1, citations[1]=a2, citations[2]=a3
    cit_aids = [c.article_id for c in result.citations]
    assert cit_aids[1] == "a2", f"citations[1] (index [2]) must be a2, got {cit_aids[1]}"
    assert cit_aids[2] == "a3", (
        f"citations[2] (index [3]) must be a3, not shifted, got {cit_aids[2]}"
    )


async def test_t6c_citations_order_matches_recall_order(env):
    """T6c: citations list order mirrors the `ordered` (published_at desc) sort.

    Article published on 2026-06-10 → citations[0]; 2026-06-09 → citations[1].
    The [N] in facts must align with this same ordering.
    """
    llm = _make_llm()
    pipeline = _make_pipeline(env, llm)
    # Supply in reverse-chronological order to verify pipeline sorts correctly
    matches = [
        _match("a_oldest", published_at="2026-06-08T00:00:00Z"),
        _match("a_newest", published_at="2026-06-10T00:00:00Z"),
        _match("a_middle", published_at="2026-06-09T00:00:00Z"),
    ]

    result = await pipeline.compute_summary(matches)

    assert result is not None
    # Recall order = published_at desc → newest, middle, oldest
    cit_aids = [c.article_id for c in result.citations]
    assert cit_aids[0] == "a_newest", f"citations[0] must be the newest article, got {cit_aids[0]}"
    assert cit_aids[1] == "a_middle", f"citations[1] must be the middle article, got {cit_aids[1]}"
    assert cit_aids[2] == "a_oldest", f"citations[2] must be the oldest article, got {cit_aids[2]}"

    # Verify facts article list also reflects this order
    prompt = _prompt_of(llm)
    pos_newest = prompt.find("[1]")
    pos_middle = prompt.find("[2]")
    pos_oldest = prompt.find("[3]")
    assert pos_newest < pos_middle < pos_oldest, (
        "Article list in facts must list [1] newest before [2] middle before [3] oldest"
    )


async def test_t6d_index_to_article_id_correspondence(env):
    """T6d: citations[N-1].article_id corresponds 1-to-1 with [N] in the facts
    article list. The article org/name at position [N] in the prompt comes from
    the same article as citations[N-1].

    Uses unique source_org per article for traceability in the prompt.
    """
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="digest")
    llm.max_prompt_chars = 2_000_000

    # Each article gets a unique org name so we can match prompt position ↔ aid
    call_counter = {"n": 0}

    async def structured_with_org(prompt, schema, *, system=None, model=None, repair_attempts=2):
        call_counter["n"] += 1
        # The article title encodes which article we're processing
        if "title-a1" in prompt:
            org = "OrgA1"
        elif "title-a2" in prompt:
            org = "OrgA2"
        else:
            org = "OrgA3"
        return schema(
            no_relevant_content=False,
            source_org=org,
            thesis=f"{org} thesis",
            claims=[{"section": "facts", "text": f"fact from {org}", "quote": "q"}],
        )

    llm.structured = AsyncMock(side_effect=structured_with_org)
    pipeline = _make_pipeline(env, llm)

    matches = [
        _match("a1", title="title-a1", published_at="2026-06-10T00:00:00Z"),
        _match("a2", title="title-a2", published_at="2026-06-09T00:00:00Z"),
        _match("a3", title="title-a3", published_at="2026-06-08T00:00:00Z"),
    ]

    result = await pipeline.compute_summary(matches)

    assert result is not None
    # citations[0].article_id = a1 (newest → index 1)
    # citations[1].article_id = a2 (middle → index 2)
    # citations[2].article_id = a3 (oldest → index 3)
    cit_aids = [c.article_id for c in result.citations]
    assert cit_aids == [
        "a1",
        "a2",
        "a3",
    ], f"citation order must be a1, a2, a3 (published_at desc), got {cit_aids}"

    prompt = _prompt_of(llm)
    # [1] must be associated with OrgA1 (which came from a1)
    # find "[1] OrgA1" in the article list section
    assert "[1] OrgA1" in prompt, (
        "Index [1] in article list must correspond to OrgA1 (citations[0]=a1)"
    )
    assert "[2] OrgA2" in prompt, (
        "Index [2] in article list must correspond to OrgA2 (citations[1]=a2)"
    )
    assert "[3] OrgA3" in prompt, (
        "Index [3] in article list must correspond to OrgA3 (citations[2]=a3)"
    )

    # No out-of-range references
    refs = _refs_in_prompt(prompt)
    assert refs.issubset({1, 2, 3}), f"All [N] refs must be in [1..3], got {refs}"


async def test_t6e_no_relevant_article_does_not_add_spurious_ref(env):
    """T6e: a no_relevant_content article must appear in the article list as [N]
    but must NOT generate any claim line referencing [N]. No claim line means
    no spurious [N] in the structured-facts section (only the article-list [N]).
    """
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="digest")
    llm.max_prompt_chars = 2_000_000

    async def structured_selective(prompt, schema, *, system=None, model=None, repair_attempts=2):
        if "FAILME" in prompt:
            raise RuntimeError("forced failure")
        return schema(
            no_relevant_content=False,
            source_org="GoodOrg",
            thesis="good thesis",
            claims=[{"section": "facts", "text": "one fact", "quote": "q1"}],
        )

    llm.structured = AsyncMock(side_effect=structured_selective)
    pipeline = _make_pipeline(env, llm)

    matches = [
        _match("a1", title="FAILME", published_at="2026-06-10T00:00:00Z"),
        _match("a2", title="ok-article", published_at="2026-06-09T00:00:00Z"),
    ]

    result = await pipeline.compute_summary(matches)

    assert result is not None
    assert result.reduce_mode == "facts_partial"
    prompt = _prompt_of(llm)
    # [1] appears in the article list (no_relevant_content article)
    assert "[1]" in prompt
    # Claim lines use "  - [N]" format; the failed article [1] must NOT have a claim line
    # (only [2] from the good article should have a claim)
    claim_lines = [ln for ln in prompt.splitlines() if ln.strip().startswith("- [")]
    ref_in_claims = set()
    for ln in claim_lines:
        m = re.search(r"\[(\d+)\]", ln)
        if m:
            ref_in_claims.add(int(m.group(1)))
    assert 1 not in ref_in_claims, (
        f"Failed article [1] must not appear in claim lines, found refs {ref_in_claims}"
    )
    assert 2 in ref_in_claims, f"Good article [2] must have claims, found refs {ref_in_claims}"

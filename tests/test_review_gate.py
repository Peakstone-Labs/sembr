# SPDX-License-Identifier: Apache-2.0
"""Unit tests for review gate (summarizer/review.py + pipeline integration).

All tests use AsyncMock LLM backends — no real API calls.  Tests are
organised by design source: D3 (correction mechanics), D4 (JSON parsing),
D5 (budget), D11 (zero-impact), D12 (unified now), D13 (Unicode).
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from sembr.summarizer.review import (
    _apply_corrections,
    _emit_review_correction,
    _nfkc,
    _parse_review_json,
    run_review_gate,
)

# ── fixtures ──────────────────────────────────────────────────────────


def _make_correction(
    quote: str,
    replacement: str,
    error_class: str = "source_attribution",
    context: str | None = None,
) -> dict:
    corr: dict = {
        "error_class": error_class,
        "quote": quote,
        "replacement": replacement,
        "cited": [1],
    }
    if context is not None:
        corr["context"] = context
    return corr


def _make_llm(responses: list[str]) -> AsyncMock:
    """Stub LLM backend: ``summarize`` returns each response in order."""
    llm = AsyncMock()
    llm.summarize = AsyncMock(side_effect=list(responses))  # copy to avoid mutation
    llm.max_prompt_chars = 200_000
    return llm


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "prompts"
    (d / "system").mkdir(parents=True)
    (d / "instruction").mkdir(parents=True)
    # Review templates
    (d / "system" / "review.md").write_text("Review system: {language}", encoding="utf-8")
    (d / "instruction" / "review.md").write_text(
        "Digest:\n{intent_text}\n\nArticles:\n{articles}", encoding="utf-8"
    )
    # Default templates (needed by pipeline for generation step)
    (d / "system" / "default.md").write_text(
        "You are an assistant. Language: {language}", encoding="utf-8"
    )
    (d / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )
    return d


# ── D13 / _nfkc ───────────────────────────────────────────────────────


def test_nfkc_normalizes_fullwidth():
    """Fullwidth comma → halfwidth: NFKC converts compatibility chars."""
    assert _nfkc("，") == ","  # fullwidth → halfwidth comma
    assert _nfkc("＂") == '"'  # fullwidth → halfwidth double-quote


# ── D4 / _parse_review_json ───────────────────────────────────────────


def test_parse_plain_json():
    assert _parse_review_json('{"corrections":[]}') == {"corrections": []}


def test_parse_fenced_json():
    assert _parse_review_json('```json\n{"corrections":[]}\n```') == {"corrections": []}


def test_parse_with_preamble():
    raw = 'Here are the fixes:\n\n{"corrections":[{"quote":"x","replacement":"y","cited":[1]}]}'
    parsed = _parse_review_json(raw)
    assert len(parsed["corrections"]) == 1


def test_parse_trailing_comma():
    raw = '{"corrections": [{"quote": "x", "replacement": "y", "cited": [1],},]}'
    parsed = _parse_review_json(raw)
    assert len(parsed["corrections"]) == 1


def test_parse_python_dict_literal_eval():
    """Single quotes + None/True — ast.literal_eval fallback."""
    raw = "{'corrections': [{'quote': 'x', 'replacement': 'y', 'cited': [1], 'error_class': None}]}"
    parsed = _parse_review_json(raw)
    assert len(parsed["corrections"]) == 1
    assert parsed["corrections"][0]["error_class"] is None


def test_parse_empty_string_raises():
    with pytest.raises(ValueError):
        _parse_review_json("not json at all")


# ── D3 / _apply_corrections ───────────────────────────────────────────


def test_apply_single_correction():
    summary = "GDP grew 5.2% [1]. Inflation is low [2]."
    corrections = [_make_correction("5.2%", "4.8%", "cross_article")]
    result, audit = _apply_corrections(summary, corrections)
    assert result == "GDP grew 4.8% [1]. Inflation is low [2]."
    assert len(audit) == 1
    assert audit[0]["matched"] is True


def test_apply_empty_corrections():
    summary = "GDP grew 5.2%. Inflation is low."
    result, audit = _apply_corrections(summary, [])
    assert result == summary
    assert audit == []


def test_apply_delete_via_empty_replacement():
    summary = "GDP grew 5.2%. This claim is fabricated [3]. Inflation is low."
    corrections = [_make_correction("This claim is fabricated [3]. ", "")]
    result, audit = _apply_corrections(summary, corrections)
    assert "fabricated" not in result
    assert audit[0]["matched"] is True


def test_apply_unmatched_quote_skipped():
    summary = "GDP grew 5.2%."
    corrections = [_make_correction("not in text", "something")]
    result, audit = _apply_corrections(summary, corrections)
    assert result == summary  # unchanged
    assert audit[0]["matched"] is False


def test_apply_multiple_corrections():
    summary = "GDP grew 5.2%. CPI rose 3.1%."
    corrections = [
        _make_correction("5.2%", "4.8%"),
        _make_correction("3.1%", "2.9%"),
    ]
    result, audit = _apply_corrections(summary, corrections)
    assert "4.8%" in result
    assert "2.9%" in result
    assert all(e["matched"] for e in audit)


def test_apply_ambiguous_quote_warns(caplog):
    """F2: when quote appears twice, logs ambiguous warning."""
    summary = "Rate was 25bp. Later, rate changed to 25bp."
    corrections = [_make_correction("25bp", "50bp")]
    with caplog.at_level(logging.WARNING):
        result, audit = _apply_corrections(summary, corrections)
    # First occurrence replaced
    assert result.startswith("Rate was 50bp.")
    assert result.endswith("rate changed to 25bp.")
    assert "ambiguous" in caplog.text.lower()


def test_apply_context_anchor_disambiguates():
    """context field narrows match to the right occurrence."""
    summary = "Rate was 25bp. Later, rate changed to 25bp."
    corrections = [_make_correction("25bp", "50bp", context="rate changed to ")]
    result, audit = _apply_corrections(summary, corrections)
    # Second occurrence (after "rate changed to ") replaced
    assert "Rate was 25bp." in result
    assert "rate changed to 50bp" in result
    assert audit[0]["matched"] is True


def test_apply_nfkc_normalization():
    """D13: LLM quote uses fullwidth comma, digest uses halfwidth — NFKC bridges them."""
    summary = "According to Fed, rates will rise."  # halfwidth comma
    quote_fullwidth = "Fed，"  # fullwidth comma (LLM output)
    corrections = [_make_correction(quote_fullwidth, "PBoC,")]
    result, audit = _apply_corrections(summary, corrections)
    assert "PBoC," in result
    assert "Fed," not in result
    assert audit[0]["matched"] is True


# ── D6 / audit ────────────────────────────────────────────────────────


def test_emit_review_correction_logs(caplog):
    with caplog.at_level(logging.WARNING):
        _emit_review_correction(1, "2026-06-19T09:00:00Z", "source_attribution", "wrong", "correct")
    assert "review_gate_audit" in caplog.text
    assert "intent_id=1" in caplog.text
    assert "run_at=2026-06-19T09:00:00Z" in caplog.text
    assert "class=source_attribution" in caplog.text


# ── run_review_gate integration ───────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_applies_correction(prompts_dir):
    """Correction returned by LLM is applied; summary is rewritten."""
    summary = "According to SourceA, GDP grew 5.2%."
    articles = "[1] SourceB: GDP grew 4.8%.\nSource: https://example.com/b"
    review_json = '{"corrections":[{"error_class":"source_attribution","quote":"SourceA","replacement":"SourceB","cited":[1]}]}'

    llm = _make_llm([review_json])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert "SourceA" not in result
    assert "SourceB" in result


@pytest.mark.asyncio
async def test_gate_zero_corrections_verbatim(prompts_dir):
    """Empty corrections → summary returned verbatim."""
    summary = "GDP grew 5.2%."
    articles = "[1] Source\nSource: https://x.com/1"
    llm = _make_llm(['{"corrections":[]}'])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == summary


@pytest.mark.asyncio
async def test_gate_llm_error_failopen(prompts_dir):
    """LLMError → original summary returned unchanged."""
    llm = AsyncMock()
    llm.summarize = AsyncMock(side_effect=RuntimeError("API down"))
    llm.max_prompt_chars = 200_000
    summary = "original"
    result = await run_review_gate(
        llm,
        1,
        summary,
        "articles",
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == "original"


@pytest.mark.asyncio
async def test_gate_bad_json_failopen(prompts_dir):
    """Non-JSON response → original returned."""
    llm = _make_llm(["just some text, not JSON at all"])
    summary = "original digest"
    result = await run_review_gate(
        llm,
        1,
        summary,
        "articles",
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == "original digest"


@pytest.mark.asyncio
async def test_gate_budget_exceeded_skips(prompts_dir):
    """Prompt over budget → skip gate, return original."""
    llm = AsyncMock()
    llm.max_prompt_chars = 50  # impossibly small
    llm.summarize = AsyncMock()
    summary = "digest text that is fairly long for a 50 char budget"
    result = await run_review_gate(
        llm,
        1,
        summary,
        "some articles",
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == summary
    llm.summarize.assert_not_called()  # never made it to LLM call


@pytest.mark.asyncio
async def test_gate_language_en_renders(prompts_dir):
    """Template renders correctly with language='en'."""
    llm = _make_llm(['{"corrections":[]}'])
    result = await run_review_gate(
        llm,
        1,
        "digest",
        "articles",
        "en",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == "digest"  # zero corrections, verbatim return


def test_apply_corrections_skips_non_dict_entries():
    """🟢-3: non-dict entries in corrections list are silently skipped."""
    corrections = [
        None,
        "string",
        123,
        _make_correction("real error", "fixed"),
    ]
    result, audit = _apply_corrections("text with real error", corrections)
    assert "fixed" in result
    assert len(audit) == 1  # only the valid dict correction


def test_apply_corrections_happy_path():
    """_apply_corrections correctly applies a single valid correction."""
    result, audit = _apply_corrections("valid text", [_make_correction("valid", "ok")])
    assert len(audit) == 1
    assert audit[0]["matched"] is True


@pytest.mark.asyncio
async def test_gate_template_missing_failopen(prompts_dir):
    """Missing review template → fail-open."""
    # Delete the instruction template to simulate missing template
    (prompts_dir / "instruction" / "review.md").unlink()
    llm = _make_llm(["{}"])
    summary = "original"
    result = await run_review_gate(
        llm,
        1,
        summary,
        "articles",
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result == "original"


@pytest.mark.asyncio
async def test_gate_audit_summary_logged(caplog, prompts_dir):
    """After corrections applied, summary log line emitted."""
    review_json = (
        '{"corrections":['
        '{"error_class":"fabricated_fact","quote":"made up claim","replacement":"","cited":[1]}'
        "]}"
    )
    llm = _make_llm([review_json])
    summary = "Some real text. made up claim. More text."
    with caplog.at_level(logging.WARNING):
        result = await run_review_gate(
            llm,
            1,
            summary,
            "articles",
            "zh",
            "2026-01-01T00:00:00Z",
            prompts_dir=str(prompts_dir),
        )
    assert "made up claim" not in result
    assert "review_gate" in caplog.text
    assert "corrections=1" in caplog.text
    assert "review_gate_audit" in caplog.text


# ── Pipeline integration tests ────────────────────────────────────────

from sembr.summarizer.pipeline import SummaryPipeline  # noqa: E402


async def _ctx(iid):
    return "default", "default", "test intent text", "zh", None


def _make_matches(intent_id: int = 1) -> list:
    """Minimal Match-like objects for pipeline tests."""
    from sembr.matcher.callback import Match

    return [
        Match(
            article_id="a1",
            intent_id=intent_id,
            payload={
                "title": "Test Article",
                "body": "Body of test article with some content.",
                "url": "https://example.com/1",
                "published_at": "2026-01-01T00:00:00Z",
                "feed_id": 1,
            },
            score=0.85,
        )
    ]


@pytest.mark.asyncio
async def test_pipeline_gate_on_applies_correction(prompts_dir):
    """Flag ON → gate runs, correction applied to result.summary."""
    # LLM returns a digest then review JSON with a correction
    digest = "According to SourceX, GDP grew 5.2% [1]."
    review_json = '{"corrections":[{"error_class":"source_attribution","quote":"SourceX","replacement":"SourceB","cited":[1]}]}'
    llm = _make_llm([digest, review_json])
    gate_fetcher = AsyncMock(return_value=True)

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=gate_fetcher,
        prompts_dir=Path(prompts_dir),
    )

    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert "SourceX" not in result.summary
    assert "SourceB" in result.summary
    assert result.run_at is not None  # D12


@pytest.mark.asyncio
async def test_pipeline_gate_off_no_second_call(prompts_dir):
    """Flag OFF → only one LLM call (generation), no review."""
    digest = "All good [1]."
    llm = _make_llm([digest])  # only one response needed
    gate_fetcher = AsyncMock(return_value=False)

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=gate_fetcher,
        prompts_dir=Path(prompts_dir),
    )

    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert result.summary == digest
    assert llm.summarize.call_count == 1  # only generation, no review


@pytest.mark.asyncio
async def test_pipeline_gate_fetcher_exception_treated_off(prompts_dir):
    """get_review_gate raises → gate stays OFF (fail-open)."""
    digest = "All good [1]."
    llm = _make_llm([digest])
    failing_fetcher = AsyncMock(side_effect=RuntimeError("DB down"))

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=failing_fetcher,
        prompts_dir=Path(prompts_dir),
    )

    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert result.summary == digest
    assert llm.summarize.call_count == 1  # no review call


@pytest.mark.asyncio
async def test_pipeline_no_gate_fetcher_defaults_off(prompts_dir):
    """No get_review_gate injected → gate never runs, behaviour unchanged."""
    digest = "All good [1]."
    llm = _make_llm([digest])

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        prompts_dir=Path(prompts_dir),
        # get_review_gate omitted entirely
    )

    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert result.summary == digest
    assert llm.summarize.call_count == 1


@pytest.mark.asyncio
async def test_pipeline_run_at_in_result(prompts_dir):
    """D12: SummaryResult.run_at is set to effective_now."""
    llm = _make_llm(["digest"])
    gate_fetcher = AsyncMock(return_value=False)

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=gate_fetcher,
        prompts_dir=Path(prompts_dir),
    )

    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert result.run_at is not None
    # Format: YYYY-MM-DDTHH:MM:SSZ
    assert len(result.run_at) == 20
    assert result.run_at.endswith("Z")
    assert "T" in result.run_at


# ═══════════════════════════════════════════════════════════════════════════
# QA-owned tests
# ═══════════════════════════════════════════════════════════════════════════


# ── Golden regression: fabricated source attribution ─────────────────────


@pytest.mark.asyncio
async def test_review_gate_golden_fed_6_14(prompts_dir):
    """Golden regression (6/14): fabricated source attribution (e.g. 沧一土狗) is fixed.

    This simulates the "沧海一土狗" type error where the digest attributes a
    statement to a fabricated source name that doesn't match any article source.
    """
    summary = "According to 沧一土狗, the macro policy is expected to loosen " "in H2 2025. [1]"
    articles = "[1] Source: 建行金融市场部 analysis note on macro policy."
    review_json = (
        '{"corrections":[{"error_class":"source_attribution",'
        '"quote":"沧一土狗","replacement":"建行金融市场部","cited":[1],'
        '"context":"According to "}]}'
    )
    llm = _make_llm([review_json])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert "沧一土狗" not in result, "fabricated source name must be removed"
    assert "建行金融市场部" in result, "correct source name must be present"
    assert "[1]" in result, "citation must be preserved"


# ── Zero false positive: clean digest untouched ──────────────────────────


@pytest.mark.asyncio
async def test_review_gate_clean_digest_untouched(prompts_dir):
    """Zero false positive: a clean digest with empty corrections passes through verbatim."""
    summary = (
        "According to the PBOC, interest rates remain stable. [1] This supports economic growth."
    )
    articles = "[1] Source from PBOC official release."
    llm = _make_llm(['{"corrections":[]}'])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert result is summary, "clean digest must be byte-identical (not just equal)"


# ── Cross-article number mismatch ────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_gate_cross_article_number(prompts_dir):
    """Cross-article number mismatch is corrected."""
    summary = "GDP growth reached 5.2% in Q1 [2]. Inflation remains moderate [3]."
    articles = "[1] GDP report\n[2] Inflation data"
    review_json = (
        '{"corrections":[{"error_class":"cross_article",'
        '"quote":"[2]","replacement":"[1]","cited":[1]},'
        '{"error_class":"cross_article",'
        '"quote":"[3]","replacement":"[2]","cited":[2]}]}'
    )
    llm = _make_llm([review_json])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert "[1]" in result
    assert "[2]" in result
    assert "[3]" not in result


# ── Fabricated fact ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_gate_fabricated_fact(prompts_dir):
    """Fabricated fact (content not found in any article) is deleted."""
    summary = "The government announced a 10% stimulus package. [1] Inflation remains low."
    articles = "[1] Inflation data shows stable prices."
    review_json = (
        '{"corrections":[{"error_class":"fabricated_fact",'
        '"quote":"The government announced a 10% stimulus package. ","replacement":"","cited":[1]}]}'
    )
    llm = _make_llm([review_json])
    result = await run_review_gate(
        llm,
        1,
        summary,
        articles,
        "zh",
        "2026-01-01T00:00:00Z",
        prompts_dir=str(prompts_dir),
    )
    assert "stimulus package" not in result
    assert "Inflation remains low" in result


# ── All digest output paths covered ──────────────────────────────────────


@pytest.mark.asyncio
async def test_review_gate_all_paths_covered(prompts_dir):
    """All digest output paths see the gate when flag is ON.

    Paths tested:
      1. compute_summary()  — backfill / external_fire (direct call)
      2. handle()           — cron / on_match
      3. fire_handle()      — manual fire
      4. handle(persist=False) — event-mode flush

    Each verifies the gate ran (LLM called twice, correction applied).
    """
    digest = "According to SourceX, GDP grew 5.2% [1]."
    review_json = (
        '{"corrections":[{"error_class":"source_attribution",'
        '"quote":"SourceX","replacement":"SourceB","cited":[1]}]}'
    )

    paths_tested = 0

    # Path 1: compute_summary (backfill/external_fire direct call)
    llm = _make_llm([digest, review_json])
    on_summary = AsyncMock()
    on_persist = AsyncMock()
    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=AsyncMock(return_value=True),
        on_summary=on_summary,
        on_persist=on_persist,
        prompts_dir=Path(prompts_dir),
    )
    result = await pipeline.compute_summary(_make_matches())
    assert result is not None
    assert "SourceX" not in result.summary
    assert "SourceB" in result.summary
    assert llm.summarize.call_count == 2
    paths_tested += 1

    # Path 2: handle (cron path, persist=True)
    llm = _make_llm([digest, review_json])
    on_summary2 = AsyncMock()
    on_persist2 = AsyncMock()
    pipeline2 = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=AsyncMock(return_value=True),
        on_summary=on_summary2,
        on_persist=on_persist2,
        prompts_dir=Path(prompts_dir),
    )
    await pipeline2.handle(_make_matches())
    assert llm.summarize.call_count == 2
    on_persist2.assert_awaited_once()  # persist=True → on_persist called
    # Verify the corrected summary was persisted
    persisted = on_persist2.call_args[0][0]
    assert "SourceX" not in persisted.summary
    assert "SourceB" in persisted.summary
    paths_tested += 1

    # Path 3: fire_handle (manual fire, persist=False)
    llm = _make_llm([digest, review_json])
    on_summary3 = AsyncMock()
    on_persist3 = AsyncMock()
    pipeline3 = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=AsyncMock(return_value=True),
        on_summary=on_summary3,
        on_persist=on_persist3,
        prompts_dir=Path(prompts_dir),
    )
    await pipeline3.fire_handle(_make_matches(), persist=False)
    assert llm.summarize.call_count == 2
    on_persist3.assert_not_called()  # persist=False → no persist
    on_summary3.assert_awaited_once()
    paths_tested += 1

    # Path 4: handle with persist=False (event-mode flush)
    llm = _make_llm([digest, review_json])
    on_summary4 = AsyncMock()
    on_persist4 = AsyncMock()
    pipeline4 = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=AsyncMock(return_value=True),
        on_summary=on_summary4,
        on_persist=on_persist4,
        prompts_dir=Path(prompts_dir),
    )
    await pipeline4.handle(_make_matches(), persist=False)
    assert llm.summarize.call_count == 2
    on_persist4.assert_not_called()  # persist=False
    on_summary4.assert_awaited_once()
    paths_tested += 1

    assert paths_tested == 4, f"expected 4 paths, tested {paths_tested}"


# ── History stores only corrected version ────────────────────────────────


@pytest.mark.asyncio
async def test_review_gate_history_one_version(prompts_dir):
    """History only stores the corrected version (not the raw digest).

    Structural guarantee: on_persist receives the corrected SummaryResult
    because compute_summary returns the gate-modified summary.
    """
    digest = "According to SourceX, GDP grew 5.2% [1]."
    review_json = (
        '{"corrections":[{"error_class":"source_attribution",'
        '"quote":"SourceX","replacement":"SourceB","cited":[1]}]}'
    )
    llm = _make_llm([digest, review_json])
    on_persist = AsyncMock()

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=_ctx,
        get_review_gate=AsyncMock(return_value=True),
        on_persist=on_persist,
        prompts_dir=Path(prompts_dir),
    )

    await pipeline.handle(_make_matches())

    on_persist.assert_awaited_once()
    persisted = on_persist.call_args[0][0]
    assert "SourceX" not in persisted.summary, "must not persist raw digest"
    assert "SourceB" in persisted.summary, "must persist corrected version"


# ── Frontend toggle round-trip via API ───────────────────────────────────


@pytest.mark.asyncio
async def test_review_gate_frontend_toggle_e2e():
    """Frontend toggle round-trip: create intent with review_gate=True,
    read back, verify the toggle state survives storage.

    Simulates the dashboard form flow: user toggles review_gate ON → POST
    intent with review_gate=True → dashboard reads back via GET → checkbox
    shows checked (review_gate=True).
    """
    import aiosqlite  # noqa: PLC0415

    from sembr.db.intents import create_intent, get_intent, init_intent_tables
    from sembr.db.sqlite import install_for_test
    from sembr.models import ChannelConfig, IntentCreate

    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    install_for_test(conn)

    # 1. Create with review_gate=True
    body = IntentCreate(
        name="test-gate",
        text="test content with review gate",
        channels=[ChannelConfig(type="email", to=["a@example.com"])],
        review_gate=True,
    )
    created = await create_intent(conn, body)
    assert created.review_gate is True, "create must return review_gate=True"

    # 2. Read back — GET /intents/{id} equivalent
    fetched = await get_intent(conn, created.id)
    assert fetched is not None
    assert fetched.review_gate is True, "read-back must preserve review_gate=True"
    assert fetched.name == "test-gate"

    # 3. Default (no review_gate) is False
    body_default = IntentCreate(
        name="test-default",
        text="default intent",
        channels=[ChannelConfig(type="email", to=["b@example.com"])],
    )
    created_default = await create_intent(conn, body_default)
    assert created_default.review_gate is False, "default must be False"

    # 4. Toggle OFF: explicit False
    body_off = IntentCreate(
        name="test-off",
        text="off intent",
        channels=[ChannelConfig(type="email", to=["c@example.com"])],
        review_gate=False,
    )
    created_off = await create_intent(conn, body_off)
    assert created_off.review_gate is False, "explicit False must be stored"

    await conn.close()

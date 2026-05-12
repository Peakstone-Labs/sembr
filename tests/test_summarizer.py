# SPDX-License-Identifier: Apache-2.0
"""Unit tests for summarizer modules (Windows-runnable, no Docker/GPU deps)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sembr.matcher.callback import Match
from sembr.summarizer.grouping import GroupingStep
from sembr.summarizer.pipeline import SummaryPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match(
    article_id: str, title: str, published_at: str | None = None, intent_id: int = 1
) -> Match:
    return Match(
        intent_id=intent_id,
        article_id=article_id,
        score=0.8,
        payload={
            "title": title,
            "url": f"https://example.com/{article_id}",
            "body": f"body of {article_id}",
            "published_at": published_at,
            "feed_id": 1,
        },
    )


def _make_llm(summary: str = "test summary") -> AsyncMock:
    llm = AsyncMock()
    llm.summarize = AsyncMock(return_value=summary)
    llm.max_prompt_chars = 2_000_000  # roomy default; tests can override
    return llm


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "You are an assistant. Language: {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )
    return tmp_path


async def _ctx(iid):
    return "default", "default", "fed rate", "zh"


# ---------------------------------------------------------------------------
# SC1 — Grouping correctness
# ---------------------------------------------------------------------------


def test_grouping_two_similar_one_independent() -> None:
    """3 articles: A+B same event (near-duplicate titles score ~0.96), C independent."""
    matches = [
        _match("a", "Fed raises interest rates by 25 basis points"),
        _match("b", "Fed raises interest rates by 25 basis points today"),
        _match("c", "Apple unveils new iPhone model at WWDC"),
    ]
    groups = GroupingStep(threshold=0.85).group(matches)
    assert len(groups) == 2
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 2]


def test_grouping_all_distinct() -> None:
    matches = [
        _match("a", "Fed raises rates"),
        _match("b", "Apple unveils new chip"),
        _match("c", "Oil price surges on OPEC cut"),
    ]
    groups = GroupingStep(threshold=0.85).group(matches)
    assert len(groups) == 3


def test_grouping_empty() -> None:
    assert GroupingStep().group([]) == []


def test_grouping_single() -> None:
    m = _match("a", "Headline")
    groups = GroupingStep().group([m])
    assert len(groups) == 1
    assert groups[0] == [m]


def test_grouping_transitive() -> None:
    """A~B and B~C should all end up in one group via union-find."""
    matches = [
        _match("a", "Fed raises rates by 25 basis points"),
        _match("b", "Fed raises rates by 25 basis points in March"),
        _match("c", "Fed raises rates by 25 basis points in March 2026"),
    ]
    groups = GroupingStep(threshold=0.85).group(matches)
    assert len(groups) == 1
    assert len(groups[0]) == 3


# ---------------------------------------------------------------------------
# SC2 — Citation ordering: newest first (canonical [N] reference order)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_citations_ordered_newest_first(prompts_dir: Path) -> None:
    """citations[0] (== primary) must be the article with the latest published_at."""
    matches = [
        _match(
            "a", "Fed raises interest rates by 25 basis points", published_at="2026-01-01T10:00:00Z"
        ),
        _match(
            "b",
            "Fed raises interest rates by 25 basis points in March",
            published_at="2026-01-01T11:00:00Z",
        ),
    ]
    captured = []

    async def capture(result):
        captured.append(result)

    llm = _make_llm()
    pipeline = SummaryPipeline(
        llm=llm,
        on_summary=capture,
        get_intent_prompt_ctx=_ctx,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle(matches)

    assert len(captured) == 1
    result = captured[0]
    assert [c.article_id for c in result.citations] == ["b", "a"]
    assert result.primary.article_id == "b"
    assert [c.article_id for c in result.other_sources] == ["a"]


@pytest.mark.asyncio
async def test_primary_none_published_at_sorts_last(prompts_dir: Path) -> None:
    """Articles with published_at=None should sort after articles with a value."""
    matches = [
        _match("none_a", "Fed raises interest rates by 25 basis points", published_at=None),
        _match(
            "early",
            "Fed raises interest rates by 25 basis points today",
            published_at="2026-01-01T10:00:00Z",
        ),
    ]
    captured = []

    async def capture(result):
        captured.append(result)

    llm = _make_llm()
    pipeline = SummaryPipeline(
        llm=llm,
        on_summary=capture,
        get_intent_prompt_ctx=_ctx,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle(matches)
    assert captured[0].primary.article_id == "early"


# ---------------------------------------------------------------------------
# SC3 — named instruction template reaches the LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_named_instruction_template_passed_to_llm(prompts_dir: Path) -> None:
    """Named instruction template content must reach the LLM prompt."""
    (prompts_dir / "instruction" / "custom.md").write_text(
        "用英文总结：{articles}", encoding="utf-8"
    )
    llm = _make_llm()
    captured_prompts: list[str] = []
    llm.summarize = AsyncMock(side_effect=lambda p, **_: captured_prompts.append(p) or "ok")

    async def ctx(iid):
        return "default", "custom", "fed rate", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")
    await pipeline.handle([m])

    assert captured_prompts
    assert captured_prompts[0].startswith("用英文总结：")


@pytest.mark.asyncio
async def test_default_instruction_template_includes_intent_text(prompts_dir: Path) -> None:
    """Default instruction template must inject intent_text into the LLM prompt."""
    llm = _make_llm()
    captured_prompts: list[str] = []
    llm.summarize = AsyncMock(side_effect=lambda p, **_: captured_prompts.append(p) or "ok")

    async def ctx(iid):
        return "default", "default", "Federal Reserve rate decisions", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")
    await pipeline.handle([m])

    assert captured_prompts
    assert "Federal Reserve rate decisions" in captured_prompts[0]


# ---------------------------------------------------------------------------
# SC4 — LLM failure graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_timeout_no_exception_no_on_summary(prompts_dir: Path) -> None:
    """LLM timeout must not propagate; on_summary must not be called."""
    import httpx

    llm = _make_llm()
    llm.summarize = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    on_summary = AsyncMock()

    pipeline = SummaryPipeline(
        llm=llm, on_summary=on_summary, get_intent_prompt_ctx=_ctx, prompts_dir=prompts_dir
    )
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")

    # Must not raise
    await pipeline.handle([m])
    on_summary.assert_not_called()


@pytest.mark.asyncio
async def test_llm_generic_error_no_exception_no_on_summary(prompts_dir: Path) -> None:
    llm = _make_llm()
    llm.summarize = AsyncMock(side_effect=RuntimeError("boom"))
    on_summary = AsyncMock()

    pipeline = SummaryPipeline(
        llm=llm, on_summary=on_summary, get_intent_prompt_ctx=_ctx, prompts_dir=prompts_dir
    )
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")
    await pipeline.handle([m])
    on_summary.assert_not_called()


# ---------------------------------------------------------------------------
# SC5 — pre_push_hook filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_push_hook_false_blocks_on_summary(prompts_dir: Path) -> None:
    """pre_push_hook returning False must prevent on_summary being called."""
    llm = _make_llm()
    on_summary = AsyncMock()

    async def hook(result):
        return result.intent_id != 99

    pipeline = SummaryPipeline(
        llm=llm,
        on_summary=on_summary,
        pre_push_hook=hook,
        get_intent_prompt_ctx=_ctx,
        prompts_dir=prompts_dir,
    )

    m_blocked = _match("x", "headline", published_at="2026-01-01T10:00:00Z", intent_id=99)
    await pipeline.handle([m_blocked])
    on_summary.assert_not_called()


@pytest.mark.asyncio
async def test_pre_push_hook_true_allows_on_summary(prompts_dir: Path) -> None:
    llm = _make_llm()
    on_summary = AsyncMock()

    async def hook(result):
        return result.intent_id != 99

    pipeline = SummaryPipeline(
        llm=llm,
        on_summary=on_summary,
        pre_push_hook=hook,
        get_intent_prompt_ctx=_ctx,
        prompts_dir=prompts_dir,
    )

    m_allowed = _match("y", "headline", published_at="2026-01-01T10:00:00Z", intent_id=1)
    await pipeline.handle([m_allowed])
    on_summary.assert_awaited_once()


# ---------------------------------------------------------------------------
# ctx error path — empty intent_text always skips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctx_fetch_failure_skips_tick() -> None:
    """When get_intent_prompt_ctx raises, pipeline skips entirely; LLM not called."""
    llm = _make_llm()
    on_summary = AsyncMock()

    async def failing_ctx(iid):
        raise RuntimeError("db locked")

    pipeline = SummaryPipeline(llm=llm, on_summary=on_summary, get_intent_prompt_ctx=failing_ctx)
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")
    await pipeline.handle([m])

    llm.summarize.assert_not_called()
    on_summary.assert_not_called()


@pytest.mark.asyncio
async def test_empty_intent_text_skips_tick() -> None:
    """ctx returning empty intent_text means intent gone — pipeline skips the tick."""
    llm = _make_llm()
    on_summary = AsyncMock()

    async def empty_ctx(iid):
        return "default", "default", "", "zh"

    pipeline = SummaryPipeline(llm=llm, on_summary=on_summary, get_intent_prompt_ctx=empty_ctx)
    m = _match("a", "Fed hikes", published_at="2026-01-01T10:00:00Z")
    await pipeline.handle([m])

    llm.summarize.assert_not_called()
    on_summary.assert_not_called()

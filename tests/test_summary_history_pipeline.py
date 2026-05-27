# SPDX-License-Identifier: Apache-2.0
"""Tests for memory-enhancement pipeline integration.

Covers:
  - {history} placeholder injection into compute_summary (D4, D5, D6, SC2, SC3)
  - on_persist slot: call order, isolation, fire_handle (D7, D8, D15, SC4)
  - template render with/without {history} (D4)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from sembr.matcher.callback import Match
from sembr.summarizer.pipeline import SummaryPipeline
from sembr.summarizer.templates import TemplateRenderError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _match(article_id: str = "a1", intent_id: int = 1) -> Match:
    return Match(
        intent_id=intent_id,
        article_id=article_id,
        score=0.85,
        payload={
            "title": "Test title",
            "body": "Body text",
            "url": "https://example.com",
            "feed_id": 1,
            "published_at": "2026-05-01T00:00:00Z",
        },
    )


def _make_llm(summary: str = "digest") -> MagicMock:
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value=summary)
    llm.max_prompt_chars = 2_000_000
    return llm


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "Assistant. Language: {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture()
def prompts_dir_with_history(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "Assistant. Language: {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\nHistory:\n{history}\n\n{articles}", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# render_instruction — history placeholder (D4, SC2, SC3)
# ---------------------------------------------------------------------------


def test_render_instruction_with_history(prompts_dir_with_history: Path) -> None:
    """Template with {history} gets the text injected (D4, SC2)."""
    from sembr.summarizer.templates import render_instruction  # noqa: PLC0415

    result = render_instruction(
        prompts_dir_with_history,
        "default",
        intent_text="AI news",
        articles="[1] Something\nSource: http://x.com",
        history="=== 2026-05-25 ===\nfoo summary",
    )
    assert "=== 2026-05-25 ===" in result
    assert "foo summary" in result


def test_render_instruction_without_history(prompts_dir: Path) -> None:
    """Template without {history} renders cleanly with history='' (D4, SC3)."""
    from sembr.summarizer.templates import render_instruction  # noqa: PLC0415

    result = render_instruction(
        prompts_dir,
        "default",
        intent_text="AI news",
        articles="articles body",
        history="",
    )
    assert "AI news" in result
    assert "articles body" in result


def test_try_render_history_allowed(prompts_dir_with_history: Path) -> None:
    """try_render accepts a template containing {history} without error (D4)."""
    from sembr.summarizer.templates import try_render  # noqa: PLC0415

    content = "Topic: {intent_text}\n\nHistory:\n{history}\n\n{articles}"
    try_render("instruction", content)  # must not raise


def test_render_instruction_history_braces_in_text(prompts_dir_with_history: Path) -> None:
    """history text containing {foo} must NOT cause TemplateRenderError (R1)."""
    from sembr.summarizer.templates import render_instruction  # noqa: PLC0415

    dangerous_history = "=== 2026-05-25 ===\nHere is {something} braces"
    result = render_instruction(
        prompts_dir_with_history,
        "default",
        intent_text="topic",
        articles="art",
        history=dangerous_history,
    )
    assert "{something}" in result


# ---------------------------------------------------------------------------
# compute_summary — history injection (D5, D6, SC2, SC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_history_injected_in_prompt(prompts_dir_with_history: Path) -> None:
    """get_history_text result appears in the LLM prompt (D5, D6, SC2)."""
    llm = _make_llm()
    history_text = "=== 2026-05-25 ===\nprev summary"

    async def ctx(iid):
        return "default", "default", "AI news", "zh", 7  # history_days=7

    get_history_text = AsyncMock(return_value=history_text)

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        get_history_text=get_history_text,
        prompts_dir=prompts_dir_with_history,
    )
    result = await pipeline.compute_summary([_match()])

    assert result is not None
    get_history_text.assert_awaited_once_with(1, 7)
    # The actual prompt passed to LLM must contain the history text
    call_args = llm.summarize.call_args
    prompt_arg = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
    assert history_text in prompt_arg


@pytest.mark.asyncio
async def test_pipeline_no_history_key_in_template(prompts_dir: Path) -> None:
    """Template without {history}: get_history_text is never called (SC3)."""
    llm = _make_llm()

    async def ctx(iid):
        return (
            "default",
            "default",
            "AI news",
            "zh",
            7,
        )  # history_days=7 but template lacks {history}

    get_history_text = AsyncMock(return_value="should not be called")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        get_history_text=get_history_text,
        prompts_dir=prompts_dir,
    )
    await pipeline.compute_summary([_match()])

    get_history_text.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_history_days_none_disables_fetch(prompts_dir_with_history: Path) -> None:
    """history_days=None: get_history_text is never called even if {history} in template (D3, P0-2)."""
    llm = _make_llm()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None  # history_days=None → disabled

    get_history_text = AsyncMock(return_value="should not be called")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        get_history_text=get_history_text,
        prompts_dir=prompts_dir_with_history,
    )
    result = await pipeline.compute_summary([_match()])

    assert result is not None
    get_history_text.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_history_counts_in_budget(prompts_dir_with_history: Path) -> None:
    """Long history text reduces body_budget — LLM still called (R2, D5)."""
    llm = _make_llm()
    llm.max_prompt_chars = 2_000_000
    long_history = "=== 2026-05-25 ===\n" + "x" * 10_000

    async def ctx(iid):
        return "default", "default", "AI news", "zh", 7

    get_history_text = AsyncMock(return_value=long_history)

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        get_history_text=get_history_text,
        prompts_dir=prompts_dir_with_history,
    )
    result = await pipeline.compute_summary([_match()])

    # With huge budget, result is still produced
    assert result is not None
    call_args = llm.summarize.call_args
    prompt_arg = call_args[0][0] if call_args[0] else call_args[1].get("prompt", "")
    assert long_history in prompt_arg


@pytest.mark.asyncio
async def test_pipeline_history_fetch_failure_is_logged_not_raised(
    prompts_dir_with_history: Path,
) -> None:
    """get_history_text raising → warning logged, compute proceeds with empty history."""
    llm = _make_llm()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", 7

    async def bad_history(iid, days):
        raise RuntimeError("db down")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        get_history_text=bad_history,
        prompts_dir=prompts_dir_with_history,
    )
    result = await pipeline.compute_summary([_match()])
    assert result is not None  # must not crash; history just becomes ""


# ---------------------------------------------------------------------------
# handle — on_persist call order and isolation (D7, D8, P0-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_on_persist_called_before_on_summary(prompts_dir: Path) -> None:
    """on_persist is called before on_summary (D8)."""
    call_order: list[str] = []
    llm = _make_llm()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    async def on_persist(result):
        call_order.append("persist")

    async def on_summary(result):
        call_order.append("summary")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=on_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    assert call_order == ["persist", "summary"]


@pytest.mark.asyncio
async def test_handle_on_persist_failure_no_raise(prompts_dir: Path) -> None:
    """on_persist raising → handle does NOT re-raise; on_summary is still called (D8, P0-1)."""
    llm = _make_llm()
    on_summary = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    async def bad_persist(result):
        raise RuntimeError("db error")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=bad_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    # Must not raise
    await pipeline.handle([_match()])
    # on_summary still called despite on_persist failure
    on_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_on_persist_always_called_for_cron(prompts_dir: Path) -> None:
    """handle() always calls on_persist (cron path always persists)."""
    llm = _make_llm()
    on_persist = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=on_persist,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])
    on_persist.assert_awaited_once()


# ---------------------------------------------------------------------------
# fire_handle — persist flag (D7, D9, SC4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_handle_persist_false(prompts_dir: Path) -> None:
    """fire_handle(persist=False) → on_persist NOT called; on_summary IS called (SC4)."""
    llm = _make_llm()
    on_persist = AsyncMock()
    on_summary = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=on_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    await pipeline.fire_handle([_match()], persist=False)

    on_persist.assert_not_called()
    on_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_handle_persist_true(prompts_dir: Path) -> None:
    """fire_handle(persist=True) → on_persist AND on_summary both called (SC4)."""
    llm = _make_llm()
    on_persist = AsyncMock()
    on_summary = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=on_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    await pipeline.fire_handle([_match()], persist=True)

    on_persist.assert_awaited_once()
    on_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_handle_never_raises_on_llm_error(prompts_dir: Path) -> None:
    """fire_handle is never-raise even when LLM fails (P0-3)."""
    llm = _make_llm()
    llm.summarize = AsyncMock(side_effect=RuntimeError("LLM died"))

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        prompts_dir=prompts_dir,
    )
    # Must not raise
    await pipeline.fire_handle([_match()], persist=False)


@pytest.mark.asyncio
async def test_fire_handle_never_raises_on_template_error(prompts_dir: Path) -> None:
    """fire_handle dispatches to on_template_error and does not raise (P0-3)."""
    llm = _make_llm()
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "ghost", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.fire_handle([_match()])

    on_template_error.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_handle_persist_on_persist_failure_no_raise(prompts_dir: Path) -> None:
    """fire_handle: on_persist failing with persist=True → still never-raise; on_summary called."""
    llm = _make_llm()
    on_summary = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    async def bad_persist(result):
        raise RuntimeError("db error")

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=bad_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    await pipeline.fire_handle([_match()], persist=True)
    on_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_fire_handle_empty_matches_no_ops(prompts_dir: Path) -> None:
    """fire_handle([]) returns immediately without calling anything."""
    llm = _make_llm()
    on_persist = AsyncMock()
    on_summary = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_persist=on_persist,
        on_summary=on_summary,
        prompts_dir=prompts_dir,
    )
    await pipeline.fire_handle([], persist=True)

    on_persist.assert_not_called()
    on_summary.assert_not_called()
    llm.summarize.assert_not_called()

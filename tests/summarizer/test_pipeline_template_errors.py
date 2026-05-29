# SPDX-License-Identifier: Apache-2.0
"""Tests: template errors route to on_template_error; LLM is never called."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.summarizer.pipeline import SummaryPipeline


def _make_match(intent_id: int = 1) -> MagicMock:
    m = MagicMock()
    m.intent_id = intent_id
    m.article_id = "a1"
    m.score = 0.85
    m.payload = {
        "title": "Test title",
        "body": "Body text",
        "url": "https://example.com",
        "feed_id": 1,
        "published_at": "2026-05-01T00:00:00Z",
    }
    return m


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


@pytest.mark.asyncio
async def test_missing_instruction_template_calls_on_template_error(prompts_dir: Path) -> None:
    """Missing instruction file → on_template_error called with kind='instruction'; LLM not called."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")
    llm.max_prompt_chars = 2_000_000
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "ghost", "fed rate", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_make_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[0] == 1  # intent_id
    assert args[1] == "instruction"
    assert args[2] == "ghost"
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_missing_system_template_calls_on_template_error(prompts_dir: Path) -> None:
    """Missing system file → on_template_error called with kind='system'; LLM not called."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")
    llm.max_prompt_chars = 2_000_000
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "ghost_system", "default", "fed rate", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_make_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[1] == "system"
    assert args[2] == "ghost_system"
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_render_error_in_instruction_calls_on_template_error(prompts_dir: Path) -> None:
    """Unknown placeholder in instruction template → on_template_error; LLM not called."""
    (prompts_dir / "instruction" / "bad.md").write_text(
        "Summary: {intent_text} {bad_key}", encoding="utf-8"
    )
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")
    llm.max_prompt_chars = 2_000_000
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "bad", "fed rate", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_make_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[1] == "instruction"
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_still_works(prompts_dir: Path) -> None:
    """Normal flow: both templates exist → LLM called, on_summary called."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="digest text")
    llm.max_prompt_chars = 2_000_000
    on_summary = AsyncMock()
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "default", "AI news", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        on_summary=on_summary,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    on_summary.assert_awaited_once()
    on_template_error.assert_not_called()


@pytest.mark.asyncio
async def test_no_on_template_error_callback_still_doesnt_raise(prompts_dir: Path) -> None:
    """Template error with no on_template_error callback → logs only, no exception."""
    llm = MagicMock()
    llm.summarize = AsyncMock()
    llm.max_prompt_chars = 2_000_000

    async def ctx(iid):
        return "default", "ghost", "fed rate", "zh", None

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=None,
        prompts_dir=prompts_dir,
    )
    # Must not raise
    await pipeline.handle([_make_match()])
    llm.summarize.assert_not_called()

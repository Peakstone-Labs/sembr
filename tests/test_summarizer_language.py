"""Unit tests for DD6: language injection into LLM system prompt."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.summarizer.pipeline import SummaryPipeline, _DEFAULT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Template format tests
# ---------------------------------------------------------------------------


def test_default_system_prompt_contains_language_placeholder() -> None:
    """The template must have a {language} placeholder."""
    assert "{language}" in _DEFAULT_SYSTEM_PROMPT


def test_default_system_prompt_format_en() -> None:
    rendered = _DEFAULT_SYSTEM_PROMPT.format(language="en")
    assert "Respond in language: en" in rendered
    assert "{language}" not in rendered


def test_default_system_prompt_format_zh() -> None:
    rendered = _DEFAULT_SYSTEM_PROMPT.format(language="zh")
    assert "Respond in language: zh" in rendered


def test_default_system_prompt_format_unrecognized() -> None:
    rendered = _DEFAULT_SYSTEM_PROMPT.format(language="tlh")
    assert "Respond in language: tlh" in rendered
    # Fallback instruction must survive
    assert "default to English" in rendered


# ---------------------------------------------------------------------------
# Pipeline integration: system prompt passed to LLM contains target language
# ---------------------------------------------------------------------------


def _make_match(intent_id: int = 1) -> MagicMock:
    m = MagicMock()
    m.intent_id = intent_id
    m.article_id = "a1"
    m.score = 0.85
    m.payload = {
        "title": "Test",
        "body": "Body text",
        "url": "https://example.com",
        "feed_id": 1,
        "enabled": True,
        "published_at": "2026-05-01T00:00:00Z",
    }
    return m


@pytest.mark.asyncio
async def test_pipeline_injects_language_en_into_system_prompt() -> None:
    """Pipeline passes language='en' to LLM system prompt."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary text")

    async def ctx_fetcher(intent_id: int):
        return None, "Bitcoin price movements", "en"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: en" in system


@pytest.mark.asyncio
async def test_pipeline_injects_language_zh_into_system_prompt() -> None:
    """Pipeline passes language='zh' to LLM system prompt."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary text")

    async def ctx_fetcher(intent_id: int):
        return None, "比特币价格动向", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: zh" in system


@pytest.mark.asyncio
async def test_pipeline_default_language_zh_when_no_ctx_fetcher() -> None:
    """Without ctx_fetcher, default language 'zh' is used."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")

    pipeline = SummaryPipeline(llm=llm)
    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: zh" in system


@pytest.mark.asyncio
async def test_pipeline_custom_prompt_still_uses_language() -> None:
    """Custom user prompt doesn't affect the system prompt language injection."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")

    async def ctx_fetcher(intent_id: int):
        return "Custom prompt: {intent_text}\n{articles}", "topic", "ja"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: ja" in system

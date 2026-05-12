"""Unit tests for language injection into LLM system prompt."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.summarizer.pipeline import SummaryPipeline


# ---------------------------------------------------------------------------
# Verify the on-disk default system template carries the language contract.
# ---------------------------------------------------------------------------


def test_default_system_md_contains_language_placeholder() -> None:
    repo_root = Path(__file__).parent.parent
    default_md = repo_root / "prompts" / "system" / "default.md"
    content = default_md.read_text(encoding="utf-8")
    assert "{language}" in content
    assert "default to English" in content


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


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "You are an assistant. Respond in language: {language}. If the requested language is unrecognized, default to English.",
        encoding="utf-8",
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n\n{articles}", encoding="utf-8"
    )
    return tmp_path


@pytest.mark.asyncio
async def test_pipeline_injects_language_en_into_system_prompt(prompts_dir: Path) -> None:
    """Pipeline passes language='en' to LLM system prompt."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary text")
    llm.max_prompt_chars = 2_000_000

    async def ctx_fetcher(intent_id: int):
        return "default", "default", "Bitcoin price movements", "en"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
        prompts_dir=prompts_dir,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: en" in system


@pytest.mark.asyncio
async def test_pipeline_injects_language_zh_into_system_prompt(prompts_dir: Path) -> None:
    """Pipeline passes language='zh' to LLM system prompt."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary text")
    llm.max_prompt_chars = 2_000_000

    async def ctx_fetcher(intent_id: int):
        return "default", "default", "比特币价格动向", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
        prompts_dir=prompts_dir,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: zh" in system


@pytest.mark.asyncio
async def test_pipeline_default_language_zh_when_no_ctx_fetcher(prompts_dir: Path) -> None:
    """Without ctx_fetcher, default language 'zh' is used."""
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")
    llm.max_prompt_chars = 2_000_000

    pipeline = SummaryPipeline(llm=llm, prompts_dir=prompts_dir)
    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Respond in language: zh" in system


@pytest.mark.asyncio
async def test_pipeline_named_system_template_uses_language(prompts_dir: Path) -> None:
    """Named system template language injection works."""
    (prompts_dir / "system" / "brief.md").write_text(
        "Brief assistant. Language: {language}.", encoding="utf-8"
    )
    llm = MagicMock()
    llm.summarize = AsyncMock(return_value="summary")
    llm.max_prompt_chars = 2_000_000

    async def ctx_fetcher(intent_id: int):
        return "brief", "default", "topic", "ja"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx_fetcher,
        prompts_dir=prompts_dir,
    )

    await pipeline.handle([_make_match()])

    llm.summarize.assert_awaited_once()
    _, kwargs = llm.summarize.call_args
    system = kwargs.get("system", "")
    assert "Language: ja" in system

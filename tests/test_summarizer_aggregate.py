# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sembr.summarizer.aggregate — budget truncation, placeholder, mock LLM."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.summarizer.aggregate import (
    MissingPlaceholderError,
    _render_prompt,
    _select_rows_within_budget,
    aggregate_history,
)


def _make_row(run_at: str, summary: str) -> dict:
    return {"run_at": run_at, "summary": summary, "id": 1, "intent_id": 1, "citations": []}


class TestRenderPrompt:
    def test_replaces_placeholder(self):
        result = _render_prompt("Before {history} After", "HISTORY_TEXT")
        assert result == "Before HISTORY_TEXT After"

    def test_raises_when_placeholder_missing(self):
        with pytest.raises(MissingPlaceholderError, match="must contain"):
            _render_prompt("no placeholder here", "text")

    def test_placeholder_appears_multiple_times(self):
        result = _render_prompt("{history} and {history}", "X")
        assert result == "X and X"


class TestSelectRowsWithinBudget:
    def test_all_rows_fit(self):
        rows = [
            _make_row("2026-05-28T00:00:00Z", "short"),
            _make_row("2026-05-27T00:00:00Z", "also short"),
        ]
        selected, dropped = _select_rows_within_budget(rows, 10_000)
        assert len(selected) == 2
        assert dropped == 0

    def test_newest_first_drop_oldest(self):
        rows = [
            _make_row("2026-05-28T00:00:00Z", "A" * 500),
            _make_row("2026-05-27T00:00:00Z", "B" * 500),
        ]
        selected, dropped = _select_rows_within_budget(rows, budget=520)
        assert len(selected) == 1
        assert dropped == 1
        assert selected[0]["run_at"] == "2026-05-28T00:00:00Z"

    def test_single_row_overflows_returns_empty(self):
        rows = [_make_row("2026-05-28T00:00:00Z", "X" * 10_000)]
        selected, dropped = _select_rows_within_budget(rows, budget=2)
        assert len(selected) == 0
        assert dropped == 1

    def test_empty_rows(self):
        selected, dropped = _select_rows_within_budget([], budget=1000)
        assert selected == []
        assert dropped == 0


class TestAggregateHistory:
    def _mock_llm(self, max_prompt_chars=10_000, response="mock summary"):
        llm = MagicMock()
        llm.max_prompt_chars = max_prompt_chars
        llm.summarize = AsyncMock(return_value=response)
        return llm

    @pytest.mark.asyncio
    async def test_happy_path(self):
        llm = self._mock_llm()
        rows = [
            _make_row("2026-05-28T00:00:00Z", "Day 1 summary."),
            _make_row("2026-05-27T00:00:00Z", "Day 2 summary."),
        ]
        result = await aggregate_history(llm, "Summarize:\n{history}", rows)
        assert result.summary == "mock summary"
        assert result.rows_total == 2
        assert result.rows_used == 2
        assert result.rows_dropped == 0
        llm.summarize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_rows_returns_none_summary(self):
        llm = self._mock_llm()
        result = await aggregate_history(llm, "Prompt {history}", [])
        assert result.summary is None
        assert result.rows_total == 0
        assert result.rows_used == 0
        llm.summarize.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_budget_truncation_newest_first(self):
        llm = self._mock_llm(max_prompt_chars=520)  # tight budget
        rows = [
            _make_row("2026-05-28T00:00:00Z", "A" * 300),
            _make_row("2026-05-27T00:00:00Z", "B" * 300),
        ]
        result = await aggregate_history(llm, "{history}", rows, safety_ratio=1.0)
        assert result.rows_used == 1
        assert result.rows_dropped == 1
        assert result.rows_total == 2

    @pytest.mark.asyncio
    async def test_single_row_too_large_raises(self):
        llm = self._mock_llm(max_prompt_chars=100)
        rows = [_make_row("2026-05-28T00:00:00Z", "X" * 10_000)]
        with pytest.raises(ValueError, match="prompt template too long"):
            await aggregate_history(llm, "{history}", rows, safety_ratio=1.0)

    @pytest.mark.asyncio
    async def test_missing_placeholder_raises(self):
        llm = self._mock_llm()
        rows = [_make_row("2026-05-28T00:00:00Z", "summary")]
        with pytest.raises(MissingPlaceholderError):
            await aggregate_history(llm, "no placeholder", rows)

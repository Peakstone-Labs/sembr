"""SummaryPipeline: on_match → group → LLM → hook → on_summary.

on_match must never raise; all errors are logged and the tick is silently
skipped (same contract as log_matches in matcher/callback.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sembr.summarizer.grouping import GroupingStep
from sembr.summarizer.models import Citation, OnSummaryCallback, PrePushHook, SummaryResult

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "default.txt"
_DEFAULT_PROMPT = _PROMPT_PATH.read_text(encoding="utf-8")

_BODY_TRUNCATE = 500  # chars per article in LLM prompt (D10)


async def log_summaries(result: SummaryResult) -> None:
    """Default on_summary: log summary to INFO."""
    logger.info(
        "on_summary intent_id=%d primary=%s summary=%r",
        result.intent_id,
        result.primary.article_id,
        result.summary[:120],
    )


def _build_articles_text(group: list[Match]) -> str:
    lines: list[str] = []
    for i, m in enumerate(group, 1):
        title = m.payload.get("title", "")
        body = m.payload.get("body", "")[:_BODY_TRUNCATE]
        url = m.payload.get("url", "")
        lines.append(f"[{i}] {title}\n{body}\n{url}")
    return "\n\n".join(lines)


def _pick_primary(group: list[Match]) -> Match:
    # None published_at sorts to end (D11); earliest = primary source
    return sorted(
        group,
        key=lambda m: m.payload.get("published_at") or "9999",
    )[0]


def _to_citation(m: Match) -> Citation:
    return Citation(
        article_id=m.article_id,
        title=m.payload.get("title", ""),
        url=m.payload.get("url", ""),
        source=m.payload.get("feed_id", 0),
        published_at=m.payload.get("published_at"),
    )


class SummaryPipeline:
    def __init__(
        self,
        llm: BaseLLMBackend,
        grouping_threshold: float = 0.85,
        on_summary: OnSummaryCallback | None = None,
        pre_push_hook: PrePushHook | None = None,
        get_intent_custom_prompt=None,  # async (intent_id) -> str | None
    ) -> None:
        self._llm = llm
        self._grouper = GroupingStep(threshold=grouping_threshold)
        self._on_summary: OnSummaryCallback = on_summary or log_summaries
        self._pre_push_hook = pre_push_hook
        self._get_custom_prompt = get_intent_custom_prompt

        if not llm:
            logger.warning(
                "SummaryPipeline: llm backend is None — all summaries will be silently dropped"
            )

    async def handle(self, matches: list[Match]) -> None:
        """on_match callback entry point — must never raise."""
        if not matches:
            return
        try:
            await self._handle(matches)
        except Exception:
            logger.exception("SummaryPipeline.handle unexpected error for intent_id=%s", matches[0].intent_id)

    async def _handle(self, matches: list[Match]) -> None:
        intent_id = matches[0].intent_id
        groups = self._grouper.group(matches)

        custom_prompt: str | None = None
        if self._get_custom_prompt is not None:
            try:
                custom_prompt = await self._get_custom_prompt(intent_id)
            except Exception:
                logger.warning("SummaryPipeline: could not fetch custom_prompt for intent_id=%d", intent_id)

        for group in groups:
            primary = _pick_primary(group)
            other = [m for m in group if m is not primary]
            intent_text = primary.payload.get("intent_text", "")
            articles_text = _build_articles_text(group)

            prompt = _resolve_prompt(custom_prompt, intent_text, articles_text)

            try:
                summary = await self._llm.summarize(list(group), prompt)
            except Exception as exc:
                logger.error(
                    "SummaryPipeline: LLM error for intent_id=%d group_size=%d: %s",
                    intent_id,
                    len(group),
                    exc,
                )
                continue

            result = SummaryResult(
                intent_id=intent_id,
                summary=summary,
                primary=_to_citation(primary),
                other_sources=[_to_citation(m) for m in other],
            )

            if self._pre_push_hook is not None:
                try:
                    should_push = await self._pre_push_hook(result)
                except Exception:
                    logger.exception("SummaryPipeline: pre_push_hook error, skipping result")
                    continue
                if not should_push:
                    continue

            await self._on_summary(result)


def _resolve_prompt(custom_prompt: str | None, intent_text: str, articles_text: str) -> str:
    template = custom_prompt if custom_prompt else _DEFAULT_PROMPT
    try:
        return template.format_map({"intent_text": intent_text, "articles": articles_text})
    except KeyError as exc:
        logger.warning(
            "SummaryPipeline: custom_prompt has unknown placeholder %s; falling back to default",
            exc,
        )
        return _DEFAULT_PROMPT.format_map({"intent_text": intent_text, "articles": articles_text})

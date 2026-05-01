"""SummaryPipeline: on_match → group → LLM → hook → on_summary.

on_match must never raise; all errors are logged and the tick is silently
skipped (same contract as log_matches in matcher/callback.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

import html2text as _h2t

from sembr.summarizer import templates as _templates
from sembr.summarizer.grouping import GroupingStep
from sembr.summarizer.models import Citation, OnSummaryCallback, PrePushHook, SummaryResult
from sembr.summarizer.templates import TemplateNotFoundError, TemplateRenderError

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

# Hardcoded fallback strings — kept as documentation / last-resort reference.
# Not used at runtime: template errors are routed to on_template_error, not
# silently replaced. These mirror the content of prompts/system/default.md
# and prompts/instruction/default.md respectively.
_FALLBACK_SYSTEM_PROMPT = (
    "You are a news monitoring assistant. Output Markdown only "
    "(## / ### for sub-topic headings, - or * for bullets, **bold** for emphasis). "
    "Start directly with the content — no preamble. "
    "Do not restate the topic or add a top-level title. "
    "Respond in language: {language}. If the requested language is unrecognized, default to English. "
    "Structure by event or sub-topic; length reflects news density. "
    "Merge duplicate facts; note source conflicts briefly. "
    "Do not reproduce URLs or bracketed index numbers."
)

_FALLBACK_PROMPT = (
    "The user is tracking:\n\n"
    "> {intent_text}\n\n"
    "The following articles were semantically matched to this topic. "
    "Each entry contains: the article title, full body text, and the source URL.\n\n"
    "{articles}\n\n"
    "---\n\n"
    "Write a digest of the key developments above."
)

_BODY_TRUNCATE = 1_000_000  # DeepSeek Flash V4 has 1M context; effectively no truncation

_h2t_converter = _h2t.HTML2Text()
_h2t_converter.ignore_links = True
_h2t_converter.ignore_images = True
_h2t_converter.ignore_emphasis = False
_h2t_converter.body_width = 0  # no line wrapping


def _to_plain_text(raw: str) -> str:
    if "<" in raw and ">" in raw:
        return _h2t_converter.handle(raw).strip()
    return raw.strip()


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
        body = _to_plain_text(m.payload.get("body", ""))[:_BODY_TRUNCATE]
        url = m.payload.get("url", "")
        lines.append(f"[{i}] {title}\n{body}\nSource: {url}")
    return "\n\n".join(lines)


def _to_citation(m: Match, feed_name_map: dict[int, str] | None = None) -> Citation:
    feed_id = m.payload.get("feed_id", 0)
    return Citation(
        article_id=m.article_id,
        title=m.payload.get("title", ""),
        url=m.payload.get("url", ""),
        source=feed_id,
        published_at=m.payload.get("published_at"),
        source_name=(feed_name_map or {}).get(feed_id),
    )


class IntentPromptCtxFetcher(Protocol):
    async def __call__(
        self, intent_id: int
    ) -> tuple[str, str, str, str]: ...
    # Returns: (system_template_name, instruction_template_name, intent_text, language)


class FeedNameFetcher(Protocol):
    async def __call__(self, feed_ids: list[int]) -> dict[int, str]: ...


OnTemplateError = Callable[[int, str, str, str], Awaitable[None]]


class SummaryPipeline:
    def __init__(
        self,
        llm: BaseLLMBackend,
        grouping_threshold: float = 0.85,
        on_summary: OnSummaryCallback | None = None,
        pre_push_hook: PrePushHook | None = None,
        get_intent_prompt_ctx: IntentPromptCtxFetcher | None = None,
        get_feed_names: FeedNameFetcher | None = None,
        on_template_error: OnTemplateError | None = None,
        prompts_dir: Path = Path("/app/prompts"),
    ) -> None:
        self._llm = llm
        self._grouper = GroupingStep(threshold=grouping_threshold)
        self._on_summary: OnSummaryCallback = on_summary or log_summaries
        self._pre_push_hook = pre_push_hook
        self._get_intent_prompt_ctx = get_intent_prompt_ctx
        self._get_feed_names = get_feed_names
        self._on_template_error = on_template_error
        self._prompts_dir = prompts_dir

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
        ordered = sorted(
            matches,
            key=lambda m: (m.payload.get("published_at") or "", m.article_id),
            reverse=True,
        )
        groups = [ordered]

        system_tpl_name: str = "default"
        instruction_tpl_name: str = "default"
        intent_text: str = ""
        language: str = "zh"
        if self._get_intent_prompt_ctx is not None:
            try:
                system_tpl_name, instruction_tpl_name, intent_text, language = (
                    await self._get_intent_prompt_ctx(intent_id)
                )
            except Exception:
                logger.warning(
                    "SummaryPipeline: could not fetch prompt ctx for intent_id=%d, skipping tick",
                    intent_id,
                )
                return
            if not intent_text:
                logger.warning(
                    "SummaryPipeline: empty intent_text for intent_id=%d, skipping",
                    intent_id,
                )
                return

        # Render system prompt once per tick (shared across all groups).
        try:
            system_prompt = _templates.render_system(
                self._prompts_dir, system_tpl_name, language=language
            )
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            logger.error(
                "SummaryPipeline: system template error for intent_id=%d: %s",
                intent_id,
                exc,
            )
            if self._on_template_error is not None:
                await self._on_template_error(intent_id, "system", system_tpl_name, str(exc))
            return

        feed_name_map: dict[int, str] = {}
        if self._get_feed_names is not None:
            try:
                feed_ids = sorted({m.payload.get("feed_id", 0) for m in matches})
                feed_name_map = await self._get_feed_names(feed_ids)
            except Exception as exc:
                logger.warning(
                    "SummaryPipeline: feed name lookup failed for intent_id=%d: %s",
                    intent_id,
                    exc,
                )

        for group in groups:
            articles_text = _build_articles_text(group)

            try:
                prompt = _templates.render_instruction(
                    self._prompts_dir,
                    instruction_tpl_name,
                    intent_text=intent_text,
                    articles=articles_text,
                )
            except (TemplateNotFoundError, TemplateRenderError) as exc:
                logger.error(
                    "SummaryPipeline: instruction template error for intent_id=%d: %s",
                    intent_id,
                    exc,
                )
                if self._on_template_error is not None:
                    await self._on_template_error(
                        intent_id, "instruction", instruction_tpl_name, str(exc)
                    )
                return

            try:
                summary = await self._llm.summarize(prompt, system=system_prompt)
            except Exception as exc:
                logger.error(
                    "SummaryPipeline: LLM error for intent_id=%d group_size=%d: %s",
                    intent_id,
                    len(group),
                    exc,
                )
                continue

            citations = [_to_citation(m, feed_name_map) for m in group]
            result = SummaryResult(
                intent_id=intent_id,
                summary=summary,
                citations=citations,
                primary=citations[0] if citations else None,
                other_sources=citations[1:] if len(citations) > 1 else [],
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

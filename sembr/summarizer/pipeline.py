"""SummaryPipeline: on_match → group → LLM → hook → on_summary.

on_match must never raise; all errors are logged and the tick is silently
skipped (same contract as log_matches in matcher/callback.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import html2text as _h2t

from sembr.summarizer.grouping import GroupingStep
from sembr.summarizer.models import Citation, OnSummaryCallback, PrePushHook, SummaryResult

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

# Hardcoded fallback ensures import never fails on a partial/broken install.
_FALLBACK_PROMPT = (
    "You are a news monitoring assistant. The user is tracking:\n\n"
    "> {intent_text}\n\n"
    "The following articles were semantically matched to this topic. "
    "Each entry contains: the article title, full body text, and the source URL.\n\n"
    "{articles}\n\n"
    "---\n\n"
    "Write a digest of the key developments. Structure by event or sub-topic; "
    "length should reflect news density (no padding, no over-truncation). "
    "Respond in the same language as the user's topic (not the articles — articles may be mixed-language). "
    "Merge duplicate facts across sources; note conflicts briefly. "
    "Do not reproduce URLs or the bracketed index numbers."
)

try:
    _DEFAULT_PROMPT = (Path(__file__).parent / "prompts" / "default.md").read_text(encoding="utf-8")
except (FileNotFoundError, OSError) as _exc:
    logger.warning("default.txt prompt template not found (%s); using built-in fallback", _exc)
    _DEFAULT_PROMPT = _FALLBACK_PROMPT

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


def _pick_primary(group: list[Match]) -> Match:
    # D11: earliest published_at = primary; None values sort last via the
    # (is_none, value) tuple — explicit, immune to lexicographic-sentinel pitfalls.
    return sorted(
        group,
        key=lambda m: (m.payload.get("published_at") is None, m.payload.get("published_at") or ""),
    )[0]


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
    async def __call__(self, intent_id: int) -> tuple[str | None, str]: ...


class FeedNameFetcher(Protocol):
    async def __call__(self, feed_ids: list[int]) -> dict[int, str]: ...


class SummaryPipeline:
    def __init__(
        self,
        llm: BaseLLMBackend,
        grouping_threshold: float = 0.85,
        on_summary: OnSummaryCallback | None = None,
        pre_push_hook: PrePushHook | None = None,
        get_intent_prompt_ctx: IntentPromptCtxFetcher | None = None,
        get_feed_names: FeedNameFetcher | None = None,
    ) -> None:
        self._llm = llm
        self._grouper = GroupingStep(threshold=grouping_threshold)
        self._on_summary: OnSummaryCallback = on_summary or log_summaries
        self._pre_push_hook = pre_push_hook
        self._get_intent_prompt_ctx = get_intent_prompt_ctx
        self._get_feed_names = get_feed_names

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
        # One scan = one digest email per intent. The LLM (1M ctx) deduplicates
        # near-identical reports across sources itself, so the title-similarity
        # GroupingStep is bypassed; kept on disk for future per-event mode.
        groups = [matches]

        custom_prompt: str | None = None
        intent_text: str = ""
        if self._get_intent_prompt_ctx is not None:
            try:
                custom_prompt, intent_text = await self._get_intent_prompt_ctx(intent_id)
            except Exception:
                logger.warning(
                    "SummaryPipeline: could not fetch prompt ctx for intent_id=%d, skipping tick",
                    intent_id,
                )
                return
            # An empty topic line + no custom prompt produces a useless LLM input.
            # Treat as "intent gone" (deleted between match and summarize) and skip.
            if not custom_prompt and not intent_text:
                logger.warning(
                    "SummaryPipeline: empty intent_text and no custom_prompt for intent_id=%d, skipping",
                    intent_id,
                )
                return

        feed_name_map: dict[int, str] = {}
        if self._get_feed_names is not None:
            try:
                feed_ids = sorted({m.payload.get("feed_id", 0) for m in matches})
                feed_name_map = await self._get_feed_names(feed_ids)
            except Exception as exc:
                # Citations fall back to source_name=None; not a hard failure.
                logger.warning(
                    "SummaryPipeline: feed name lookup failed for intent_id=%d: %s",
                    intent_id,
                    exc,
                )

        for group in groups:
            primary = _pick_primary(group)
            other = [m for m in group if m is not primary]
            articles_text = _build_articles_text(group)

            prompt = _resolve_prompt(custom_prompt, intent_text, articles_text)

            try:
                summary = await self._llm.summarize(prompt)
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
                primary=_to_citation(primary, feed_name_map),
                other_sources=[_to_citation(m, feed_name_map) for m in other],
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
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "SummaryPipeline: custom_prompt render failed (%s); falling back to default",
            exc,
        )
        return _DEFAULT_PROMPT.format_map({"intent_text": intent_text, "articles": articles_text})

"""SummaryPipeline: on_match → LLM → hook → on_summary.

on_match must never raise; all errors are logged and the tick is silently
skipped (same contract as log_matches in matcher/callback.py).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol

import html2text as _h2t

from sembr.summarizer import templates as _templates
from sembr.summarizer.models import Citation, OnSummaryCallback, PrePushHook, SummaryResult
from sembr.summarizer.templates import TemplateNotFoundError, TemplateRenderError

if TYPE_CHECKING:
    from sembr.matcher.callback import Match
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_h2t_converter = _h2t.HTML2Text()
_h2t_converter.ignore_links = True
_h2t_converter.ignore_images = True
_h2t_converter.ignore_emphasis = False
_h2t_converter.body_width = 0  # no line wrapping

# Reserve 15% of the backend's prompt budget for the LLM's response and any
# instruction-template overhead the pipeline can't measure ahead of time.
# Articles get the remaining 85%.
_BUDGET_SAFETY_RATIO = 0.85
_ENTRY_SEPARATOR = "\n\n"


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


def _entry_overhead(i: int, title: str, url: str) -> int:
    """Per-entry character cost excluding the body — must mirror _format_entry."""
    return len(f"[{i}] {title}\n\nSource: {url}")


def _format_entry(i: int, title: str, body: str, url: str) -> str:
    return f"[{i}] {title}\n{body}\nSource: {url}"


def _water_fill_cap(body_lens: list[int], budget: int) -> int | None:
    """Return the cap level such that ``sum(min(cap, n) for n in body_lens) <= budget``.

    Returns None when no truncation is needed (budget covers every body whole).
    Returns 0 when budget is negative — caller is responsible for treating that
    as "drop the bodies entirely". Articles already shorter than the cap stay
    whole; only bodies exceeding the cap get truncated, so a tick of mostly
    short articles plus one outlier only loses content from the outlier.
    """
    if budget < 0:
        return 0
    if not body_lens or sum(body_lens) <= budget:
        return None
    remaining = budget
    sorted_lens = sorted(body_lens)
    for rank, length in enumerate(sorted_lens):
        n_left = len(sorted_lens) - rank
        if length * n_left > remaining:
            return remaining // n_left
        remaining -= length
    return None  # unreachable given the sum-fits-budget short-circuit above


def _build_articles_text(
    matches: list[Match], body_budget: int
) -> tuple[str, int, int]:
    """Assemble the articles block; water-fill bodies into body_budget.

    Returns (assembled_text, n_truncated, longest_truncated_to). When
    body_budget is 0 or negative, every body is dropped (cap=0) — caller is
    expected to have already logged the over-budget condition.
    """
    bodies = [_to_plain_text(m.payload.get("body", "")) for m in matches]
    cap = _water_fill_cap([len(b) for b in bodies], body_budget)
    n_truncated = 0
    longest_truncated_to = 0
    entries: list[str] = []
    for i, (m, body) in enumerate(zip(matches, bodies), 1):
        if cap is not None and len(body) > cap:
            body = body[:cap]
            n_truncated += 1
            longest_truncated_to = max(longest_truncated_to, cap)
        title = m.payload.get("title", "")
        url = m.payload.get("url", "")
        entries.append(_format_entry(i, title, body, url))
    return _ENTRY_SEPARATOR.join(entries), n_truncated, longest_truncated_to


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
    """Per-intent: render prompt → call LLM → emit one SummaryResult per tick.

    The pipeline is intentionally one-summary-per-intent-per-tick. The matcher
    delivers everything that scored above threshold in this scan window as a
    single batch; the LLM groups and structures the digest itself in the
    output, so the pipeline does not pre-cluster on its own. Cross-source
    near-duplicate dedup, when needed at all, is the LLM's job under the
    system prompt.
    """

    def __init__(
        self,
        llm: BaseLLMBackend,
        on_summary: OnSummaryCallback | None = None,
        pre_push_hook: PrePushHook | None = None,
        get_intent_prompt_ctx: IntentPromptCtxFetcher | None = None,
        get_feed_names: FeedNameFetcher | None = None,
        on_template_error: OnTemplateError | None = None,
        prompts_dir: Path = Path("/app/prompts"),
    ) -> None:
        self._llm = llm
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

        # Render the instruction template with an empty articles slot so we know
        # how many characters the wrapper itself consumes; we'll re-render with
        # the real articles block once we've sized it to fit.
        try:
            instruction_wrapper = _templates.render_instruction(
                self._prompts_dir,
                instruction_tpl_name,
                intent_text=intent_text,
                articles="",
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

        n_articles = len(ordered)
        per_entry_overhead = sum(
            _entry_overhead(
                i, m.payload.get("title", ""), m.payload.get("url", "")
            )
            for i, m in enumerate(ordered, 1)
        )
        separator_overhead = (
            (n_articles - 1) * len(_ENTRY_SEPARATOR) if n_articles > 1 else 0
        )
        total_budget = int(self._llm.max_prompt_chars * _BUDGET_SAFETY_RATIO)
        body_budget = (
            total_budget
            - len(system_prompt)
            - len(instruction_wrapper)
            - per_entry_overhead
            - separator_overhead
        )

        if body_budget < 0:
            logger.error(
                "SummaryPipeline: intent_id=%d max_prompt_chars=%d cannot fit "
                "system+instruction+%d article headers (deficit=%d chars); "
                "skipping tick. Reduce template size or raise SEMBR_LLM_MAX_PROMPT_CHARS.",
                intent_id, self._llm.max_prompt_chars, n_articles, -body_budget,
            )
            return

        articles_text, n_truncated, longest_cap = _build_articles_text(ordered, body_budget)
        if n_truncated > 0:
            logger.warning(
                "SummaryPipeline: intent_id=%d batch_size=%d truncated %d article "
                "body/bodies to fit max_prompt_chars=%d (water-fill cap=%d chars)",
                intent_id, n_articles, n_truncated,
                self._llm.max_prompt_chars, longest_cap,
            )

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
                "SummaryPipeline: LLM error for intent_id=%d batch_size=%d: %s",
                intent_id,
                len(ordered),
                exc,
            )
            return

        citations = [_to_citation(m, feed_name_map) for m in ordered]
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
                return
            if not should_push:
                return

        await self._on_summary(result)

# SPDX-License-Identifier: Apache-2.0
"""SummaryPipeline: on_match → LLM → hook → on_summary.

on_match must never raise; all errors are logged and the tick is silently
skipped (same contract as log_matches in matcher/callback.py).

`compute_summary` is the public, raise-on-error variant: it returns a
SummaryResult or None (the None branches are configuration-level skips, not
errors), and re-raises TemplateError / LLM exceptions so synchronous callers
(e.g. external fire endpoint) can surface them. `handle` wraps it as the
never-raise on_match callback used by the cron scheduler.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

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


def _build_articles_text(matches: list[Match], body_budget: int) -> tuple[str, int, int]:
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
    for i, (m, body) in enumerate(zip(matches, bodies, strict=True), 1):
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
        score=m.score,
    )


class IntentPromptCtxFetcher(Protocol):
    async def __call__(self, intent_id: int) -> tuple[str, str, str, str, int | None]: ...

    # Returns: (system_template_name, instruction_template_name, intent_text, language, history_days)


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
        get_history_text: Callable[[int, int, datetime | None], Awaitable[str]] | None = None,
        on_persist: OnSummaryCallback | None = None,
    ) -> None:
        self._llm = llm
        self._on_summary: OnSummaryCallback = on_summary or log_summaries
        self._pre_push_hook = pre_push_hook
        self._get_intent_prompt_ctx = get_intent_prompt_ctx
        self._get_feed_names = get_feed_names
        self._on_template_error = on_template_error
        self._prompts_dir = prompts_dir
        self._get_history_text = get_history_text
        self._on_persist = on_persist

    async def compute_summary(
        self,
        matches: list[Match],
        now: datetime | None = None,
    ) -> SummaryResult | None:
        """Build prompt, call LLM, return SummaryResult — or None for skip-class
        conditions (empty matches / empty intent_text / ctx fetch failed /
        body_budget deficit). Template errors raise TemplateNotFoundError /
        TemplateRenderError; LLM errors raise the original exception.

        ``now`` threads a simulated current time down to ``get_history_text``;
        backfill replays of past fire-times pass ``now=past_fire_time`` so the
        ``{history}`` slot is anchored to that moment instead of wall-clock.
        Normal cron / external-fire callers omit it.

        Skip-class (None) vs. error-class (raise) split:
          * None  — prompt cannot be assembled but it isn't a code-path failure
                    (intent_text is empty / ctx fetch failed / batch can't fit
                    inside max_prompt_chars). Same semantics as the old
                    _handle's "logger.warning + return". Synchronous callers
                    treat these as "no summary, no error".
          * raise — template missing/broken or LLM call failed. Synchronous
                    callers convert into a `summary_error` field; `handle()`
                    catches Template* into on_template_error and swallows the
                    rest.

        Template exceptions are tagged with private
        ``_sembr_template_kind`` (``"system"`` / ``"instruction"``) and
        ``_sembr_template_name`` (the template identifier) attributes before
        re-raise. ``handle()`` reads them via ``getattr(..., "unknown")``
        when dispatching to ``on_template_error`` so it doesn't have to
        re-parse exception messages. External callers (e.g. the
        ``/api/external/.../fire`` endpoint) ignore these attributes and
        format the exception via their own scrubbing pipeline.
        """
        if not matches:
            return None

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
        history_days: int | None = None
        if self._get_intent_prompt_ctx is not None:
            try:
                (
                    system_tpl_name,
                    instruction_tpl_name,
                    intent_text,
                    language,
                    history_days,
                ) = await self._get_intent_prompt_ctx(intent_id)
            except Exception:
                logger.warning(
                    "SummaryPipeline: could not fetch prompt ctx for intent_id=%d, skipping tick",
                    intent_id,
                )
                return None
            if not intent_text:
                logger.warning(
                    "SummaryPipeline: empty intent_text for intent_id=%d, skipping",
                    intent_id,
                )
                return None

        # Template errors propagate to the outer never-raise wrapper. Logging
        # here mirrors the original _handle line so log readers see the same
        # string before the wrapper decides whether to dispatch
        # _on_template_error. We tag the exception with
        # ``_sembr_template_kind/_name`` so handle() can hand it to
        # _on_template_error without re-parsing the exception message.
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
            exc._sembr_template_kind = "system"
            exc._sembr_template_name = system_tpl_name
            raise

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

        # Load raw template to check if {history} is present, then fetch history
        # text so its char count is included in instruction_wrapper overhead.
        # Both load and render are wrapped together so TemplateNotFoundError /
        # TemplateRenderError get tagged and propagated consistently.
        history_text = ""
        try:
            raw_instruction = _templates.load_template(
                self._prompts_dir, "instruction", instruction_tpl_name
            )
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            logger.error(
                "SummaryPipeline: instruction template error for intent_id=%d: %s",
                intent_id,
                exc,
            )
            exc._sembr_template_kind = "instruction"
            exc._sembr_template_name = instruction_tpl_name
            raise
        if "{history}" in raw_instruction and self._get_history_text and history_days is not None:
            try:
                history_text = await self._get_history_text(intent_id, history_days, now)
            except Exception as exc:
                logger.warning(
                    "SummaryPipeline: history fetch failed for intent_id=%d: %s",
                    intent_id,
                    exc,
                )

        # Render the instruction template with an empty articles slot so we know
        # how many characters the wrapper itself consumes; we'll re-render with
        # the real articles block once we've sized it to fit.
        # raw_instruction was already loaded above — reuse it to avoid a second
        # disk read (render_instruction_from_raw applies _StrictMap directly).
        try:
            instruction_wrapper = _templates.render_instruction_from_raw(
                raw_instruction,
                intent_text=intent_text,
                articles="",
                history=history_text,
            )
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            logger.error(
                "SummaryPipeline: instruction template error for intent_id=%d: %s",
                intent_id,
                exc,
            )
            exc._sembr_template_kind = "instruction"
            exc._sembr_template_name = instruction_tpl_name
            raise

        n_articles = len(ordered)
        per_entry_overhead = sum(
            _entry_overhead(i, m.payload.get("title", ""), m.payload.get("url", ""))
            for i, m in enumerate(ordered, 1)
        )
        separator_overhead = (n_articles - 1) * len(_ENTRY_SEPARATOR) if n_articles > 1 else 0
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
                intent_id,
                self._llm.max_prompt_chars,
                n_articles,
                -body_budget,
            )
            return None

        articles_text, n_truncated, longest_cap = _build_articles_text(ordered, body_budget)
        if n_truncated > 0:
            logger.warning(
                "SummaryPipeline: intent_id=%d batch_size=%d truncated %d article "
                "body/bodies to fit max_prompt_chars=%d (water-fill cap=%d chars)",
                intent_id,
                n_articles,
                n_truncated,
                self._llm.max_prompt_chars,
                longest_cap,
            )

        try:
            prompt = _templates.render_instruction_from_raw(
                raw_instruction,
                intent_text=intent_text,
                articles=articles_text,
                history=history_text,
            )
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            logger.error(
                "SummaryPipeline: instruction template error for intent_id=%d: %s",
                intent_id,
                exc,
            )
            exc._sembr_template_kind = "instruction"
            exc._sembr_template_name = instruction_tpl_name
            raise

        summary = await self._llm.summarize(prompt, system=system_prompt)

        citations = [_to_citation(m, feed_name_map) for m in ordered]
        return SummaryResult(
            intent_id=intent_id,
            summary=summary,
            citations=citations,
            primary=citations[0] if citations else None,
            other_sources=citations[1:] if len(citations) > 1 else [],
        )

    async def handle(self, matches: list[Match], *, persist: bool = True) -> None:
        """on_match callback entry point — must never raise.

        Two nested try blocks split the responsibility:

        * The OUTER try wraps **only** ``compute_summary`` so its own
          template / LLM errors route correctly. Catch order is hard-coded:
          ``(TemplateNotFoundError, TemplateRenderError)`` first → dispatch
          ``on_template_error``; then bare ``Exception`` → swallow with
          ``logger.exception``. Reversing the order would let the generic
          catch eat template errors and silently drop the
          ``on_template_error`` → email path.

        * The INNER try wraps ``pre_push_hook`` + ``on_summary``. Their
          exceptions are dispatch-side failures; routing them to the
          template-error channel would publish bogus
          ``kind="unknown"/name="unknown"`` alerts when, e.g., a hook does
          its own Jinja render and raises ``TemplateRenderError`` for
          unrelated reasons.

        ``compute_summary`` tags template exceptions with
        ``_sembr_template_kind`` / ``_sembr_template_name`` private
        attributes; the outer catch reads them with ``getattr(..., "unknown")``
        so a third-party caller raising one of these exception types directly
        won't crash this method, just lose the kind/name detail in the alert.
        """
        if not matches:
            return
        intent_id = matches[0].intent_id
        try:
            result = await self.compute_summary(matches)
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            kind = getattr(exc, "_sembr_template_kind", "unknown")
            name = getattr(exc, "_sembr_template_name", "unknown")
            if self._on_template_error is not None:
                try:
                    await self._on_template_error(intent_id, kind, name, str(exc))
                except Exception:
                    logger.exception(
                        "SummaryPipeline: on_template_error callback raised for intent_id=%d",
                        intent_id,
                    )
            return
        except Exception:
            logger.exception(
                "SummaryPipeline.compute_summary unexpected error for intent_id=%s", intent_id
            )
            return

        if result is None:
            return

        await self._dispatch(result, persist=persist, intent_id=intent_id)

    async def _dispatch(
        self,
        result: SummaryResult,
        *,
        persist: bool,
        intent_id: int,
    ) -> None:
        """Inner-try dispatch shared by handle() and fire_handle().

        Encapsulates pre_push_hook + optional on_persist + on_summary in a
        single never-raise block.  on_persist failures are isolated so they
        cannot block on_summary.
        """
        # Persist first — history is a system-of-record concern, independent of
        # downstream dispatch decisions like pre_push_hook dedup.
        if persist and self._on_persist is not None:
            try:
                await self._on_persist(result)
            except Exception:
                logger.exception("SummaryPipeline on_persist failed for intent_id=%s", intent_id)
        try:
            if self._pre_push_hook is not None and not await self._pre_push_hook(result):
                return
            await self._on_summary(result)
        except Exception:
            logger.exception(
                "SummaryPipeline dispatch (pre_push_hook / on_summary) error for intent_id=%s",
                intent_id,
            )

    async def fire_handle(self, matches: list[Match], persist: bool = False) -> None:
        """Never-raise variant for fire endpoints.

        Outer try wraps compute_summary (template/LLM errors); inner dispatch
        via _dispatch with persist flag.
        """
        if not matches:
            return
        intent_id = matches[0].intent_id
        try:
            result = await self.compute_summary(matches)
        except (TemplateNotFoundError, TemplateRenderError) as exc:
            kind = getattr(exc, "_sembr_template_kind", "unknown")
            name = getattr(exc, "_sembr_template_name", "unknown")
            if self._on_template_error is not None:
                try:
                    await self._on_template_error(intent_id, kind, name, str(exc))
                except Exception:
                    logger.exception(
                        "SummaryPipeline: on_template_error callback raised for intent_id=%d",
                        intent_id,
                    )
            return
        except Exception:
            logger.exception(
                "SummaryPipeline.fire_handle compute_summary unexpected error for intent_id=%s",
                intent_id,
            )
            return

        if result is None:
            return

        await self._dispatch(result, persist=persist, intent_id=intent_id)

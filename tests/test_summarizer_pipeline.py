# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``SummaryPipeline.compute_summary`` + ``handle`` (refactor
introduced by external-fire-api feature).

Coverage map (design.md → AC#):
  * D-A7 contract — empty-matches / empty intent_text / budget-deficit /
    template error / LLM error / happy path branches of ``compute_summary``;
  * D17 / R7 — ``handle`` catch order: TemplateError dispatched to
    ``on_template_error`` (still works after refactor), generic Exception
    swallowed, never raises;
  * regression — ``handle`` is the previous on_match contract; cron-mode
    callers must keep observing it.

Existing tests in ``tests/summarizer/test_pipeline_template_errors.py`` and
``tests/test_summarizer.py`` still cover the cron happy path; this file
focuses on the new public API surface and the catch-order assertion the
review/grep stage will check.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from sembr.matcher.callback import Match
from sembr.summarizer.pipeline import SummaryPipeline
from sembr.summarizer.templates import TemplateNotFoundError


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


# ---------------------------------------------------------------------------
# compute_summary — None branches (skip-class)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_summary_returns_none_for_empty_matches(prompts_dir: Path) -> None:
    pipeline = SummaryPipeline(llm=_make_llm(), prompts_dir=prompts_dir)
    assert await pipeline.compute_summary([]) is None


@pytest.mark.asyncio
async def test_compute_summary_returns_none_for_empty_intent_text(prompts_dir: Path) -> None:
    """D-A7 / D-A8 / D15: empty intent_text → return None (not raise)."""
    llm = _make_llm()

    async def ctx(iid):
        return "default", "default", "", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    result = await pipeline.compute_summary([_match()])
    assert result is None
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_compute_summary_returns_none_when_ctx_fetch_raises(prompts_dir: Path) -> None:
    """D-A7: get_intent_prompt_ctx exception → return None (not raise)."""
    llm = _make_llm()

    async def bad_ctx(iid):
        raise RuntimeError("db down")

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=bad_ctx, prompts_dir=prompts_dir)
    assert await pipeline.compute_summary([_match()]) is None
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_compute_summary_returns_none_for_budget_deficit(prompts_dir: Path) -> None:
    """D-A7: max_prompt_chars too small to fit prompt scaffold → return None."""
    llm = _make_llm()
    llm.max_prompt_chars = 10  # forces body_budget < 0

    async def ctx(iid):
        return "default", "default", "AI news", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    result = await pipeline.compute_summary([_match()])
    assert result is None
    llm.summarize.assert_not_called()


# ---------------------------------------------------------------------------
# compute_summary — raise branches (template / LLM)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compute_summary_raises_template_not_found(prompts_dir: Path) -> None:
    """D-A7: missing instruction template → TemplateNotFoundError propagates."""
    llm = _make_llm()

    async def ctx(iid):
        return "default", "ghost", "topic", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    with pytest.raises(TemplateNotFoundError):
        await pipeline.compute_summary([_match()])
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_compute_summary_raises_on_llm_error(prompts_dir: Path) -> None:
    """D-A7: LLM exception (TimeoutException etc.) propagates from compute_summary."""
    llm = _make_llm()
    llm.summarize = AsyncMock(side_effect=httpx.TimeoutException("upstream stalled"))

    async def ctx(iid):
        return "default", "default", "AI news", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    with pytest.raises(httpx.TimeoutException):
        await pipeline.compute_summary([_match()])


@pytest.mark.asyncio
async def test_compute_summary_returns_summary_result(prompts_dir: Path) -> None:
    llm = _make_llm("the digest")

    async def ctx(iid):
        return "default", "default", "AI news", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    result = await pipeline.compute_summary([_match()])
    assert result is not None
    assert result.summary == "the digest"
    assert result.intent_id == 1
    assert len(result.citations) == 1


# ---------------------------------------------------------------------------
# handle — never-raise contract + template error dispatch (R7 / D17)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_still_never_raises_after_refactor(prompts_dir: Path) -> None:
    """LLM raises → handle MUST swallow (never-raise contract for on_match)."""
    llm = _make_llm()
    llm.summarize = AsyncMock(side_effect=RuntimeError("LLM died"))

    async def ctx(iid):
        return "default", "default", "AI news", "zh"

    pipeline = SummaryPipeline(llm=llm, get_intent_prompt_ctx=ctx, prompts_dir=prompts_dir)
    # Must not raise.
    await pipeline.handle([_match()])


@pytest.mark.asyncio
async def test_handle_calls_on_template_error_on_render_fail(prompts_dir: Path) -> None:
    """R7 / D17: template error path still routes to on_template_error after
    the compute_summary/handle split."""
    (prompts_dir / "instruction" / "bad.md").write_text(
        "Summary: {intent_text} {bad_key}", encoding="utf-8"
    )
    llm = _make_llm()
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "bad", "topic", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[0] == 1  # intent_id
    assert args[1] == "instruction"
    assert args[2] == "bad"
    llm.summarize.assert_not_called()


@pytest.mark.asyncio
async def test_handle_calls_on_template_error_on_missing_template(prompts_dir: Path) -> None:
    """TemplateNotFoundError must dispatch to on_template_error (not the
    generic Exception arm)."""
    llm = _make_llm()
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "default", "ghost", "topic", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[1] == "instruction"
    assert args[2] == "ghost"


@pytest.mark.asyncio
async def test_handle_pre_push_hook_template_error_does_not_dispatch_on_template_error(
    prompts_dir: Path,
) -> None:
    """🟡-2: ``on_template_error`` is the *prompt-template* error channel only.
    A hook raising ``TemplateRenderError`` (e.g. an email renderer) MUST NOT
    masquerade as a prompt-template failure — that would publish a bogus
    ``kind=unknown`` alert. The dispatch-side exception belongs in the generic
    swallowed-exception arm."""
    from sembr.summarizer.templates import TemplateRenderError  # noqa: PLC0415

    llm = _make_llm()
    on_template_error = AsyncMock()

    async def bad_hook(_):
        raise TemplateRenderError("email body render failed")

    async def ctx(iid):
        return "default", "default", "topic", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        pre_push_hook=bad_hook,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    # never-raise contract still holds + on_template_error must NOT be called.
    on_template_error.assert_not_called()


@pytest.mark.asyncio
async def test_handle_on_summary_template_error_does_not_dispatch_on_template_error(
    prompts_dir: Path,
) -> None:
    """Same defence as above but for ``on_summary``. The dispatch-side hook
    failing is not a prompt-template failure."""
    from sembr.summarizer.templates import TemplateNotFoundError  # noqa: PLC0415

    llm = _make_llm()
    on_template_error = AsyncMock()

    async def bad_summary(_):
        raise TemplateNotFoundError("email partial missing")

    async def ctx(iid):
        return "default", "default", "topic", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_summary=bad_summary,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    on_template_error.assert_not_called()


@pytest.mark.asyncio
async def test_handle_dispatches_system_render_error_correctly(
    prompts_dir: Path,
) -> None:
    """💡-2: handle-level coverage for system-template *render* errors. The
    existing test_pipeline_template_errors.py covers system NotFound + instr
    Render; this fills the matrix with system Render."""
    (prompts_dir / "system" / "broken.md").write_text(
        "Lang {language} but {bad_key}", encoding="utf-8"
    )
    llm = _make_llm()
    on_template_error = AsyncMock()

    async def ctx(iid):
        return "broken", "default", "topic", "zh"

    pipeline = SummaryPipeline(
        llm=llm,
        get_intent_prompt_ctx=ctx,
        on_template_error=on_template_error,
        prompts_dir=prompts_dir,
    )
    await pipeline.handle([_match()])

    on_template_error.assert_awaited_once()
    args = on_template_error.call_args.args
    assert args[1] == "system"
    assert args[2] == "broken"
    llm.summarize.assert_not_called()


def test_handle_catch_order_template_before_generic() -> None:
    """R7 / D17: AST-level assertion on the OUTER try/except in
    ``SummaryPipeline.handle`` — TemplateError catch must lexically precede
    the bare-Exception catch. Reversing the two would let LLM/random errors
    masquerade as template errors and break the on_template_error → email
    path. AST (not substring) is used so docstring/comment matches don't
    cause false positives or negatives.
    """
    import ast  # noqa: PLC0415
    import textwrap  # noqa: PLC0415

    src = textwrap.dedent(inspect.getsource(SummaryPipeline.handle))
    func = ast.parse(src).body[0]
    assert isinstance(func, ast.AsyncFunctionDef)

    # Find the OUTER try block (first Try statement at the function body level).
    outer_try = next((s for s in func.body if isinstance(s, ast.Try)), None)
    assert outer_try is not None, "handle() lost its outer try/except wrapper"

    handler_kinds: list[str] = []
    for handler in outer_try.handlers:
        exc_type = handler.type
        if isinstance(exc_type, ast.Tuple) and any(
            isinstance(e, ast.Name) and e.id in {"TemplateNotFoundError", "TemplateRenderError"}
            for e in exc_type.elts
        ):
            handler_kinds.append("template")
        elif isinstance(exc_type, ast.Name) and exc_type.id == "Exception":
            handler_kinds.append("generic")
        else:
            handler_kinds.append("other")

    assert "template" in handler_kinds, "handle() lost its TemplateError except clause"
    assert "generic" in handler_kinds, "handle() lost its generic Exception clause"
    assert handler_kinds.index("template") < handler_kinds.index("generic"), (
        f"handle() catch order regressed: got {handler_kinds!r}; "
        "TemplateError must be caught BEFORE bare Exception (R7 / D17)"
    )

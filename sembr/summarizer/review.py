# SPDX-License-Identifier: Apache-2.0
"""Review gate: optional LLM fact-check after digest generation.

Exposed for testing: every function except ``_emit_review_correction`` has no
side-effects (pure or log-only), so stub-LLM tests can call them directly.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sembr.summarizer.llm.base import BaseLLMBackend

logger = logging.getLogger(__name__)

_REVIEW_SYSTEM_TEMPLATE = "review"
_REVIEW_INSTRUCTION_TEMPLATE = "review"
_BUDGET_SAFETY_RATIO = 0.85
_ENTRY_SEPARATOR = "\n\n"

# Trailing comma before ] or } — JavaScript-style, rejected by json.loads.
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _nfkc(text: str) -> str:
    """Normalize to NFKC so fullwidth/halfwidth and composed/decomposed
    differences don't break exact-substring matching (D13).
    NFKC (rather than NFC) is used because fullwidth→halfwidth conversion
    (e.g. `，` → `,`) requires compatibility decomposition."""
    return unicodedata.normalize("NFKC", text)


def _parse_review_json(raw: str) -> dict:
    """6-layer JSON recovery (D4).

    Tries, in order:
      1. Strip markdown fences (```json / ```).
      2. Extract from first ``{`` to last ``}`` (handles preamble/postamble).
      3. Remove trailing commas (JavaScript-style).
      4. ``json.loads`` — the happy path.
      5. ``ast.literal_eval`` fallback (Python single-quote dicts, None/True/False).
      6. Still broken → ValueError.

    Returns the parsed dict so caller can extract ``corrections``."""
    raw_stripped = raw.strip()

    # 1. Strip markdown fences
    if raw_stripped.startswith("```"):
        # Remove opening fence line (optionally with language tag)
        raw_stripped = re.sub(r"^```(?:json)?\s*\n?", "", raw_stripped)
        # Remove closing fence
        raw_stripped = re.sub(r"\n?```\s*$", "", raw_stripped)

    # 2. Extract first { to last }
    first_brace = raw_stripped.find("{")
    last_brace = raw_stripped.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        raw_stripped = raw_stripped[first_brace : last_brace + 1]

    # 3. Remove trailing commas
    cleaned = _TRAILING_COMMA_RE.sub(r"\1", raw_stripped)

    # 4. json.loads
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # 5. ast.literal_eval fallback
    try:
        return ast.literal_eval(cleaned)
    except (ValueError, SyntaxError):
        pass

    raise ValueError(f"Failed to parse review JSON after 5 recovery layers: {raw[:200]!r}")


def _apply_corrections(summary_raw: str, corrections: list[dict]) -> tuple[str, list[dict]]:
    """Apply exact-substring patches to *summary_raw*.

    For each correction, NFC-normalises ``quote``, ``replacement``, and the
    summary, tries context-anchored match first (if ``context`` provided),
    falls back to ``str.replace(quote, replacement, 1)``.

    Returns ``(corrected_summary, audit_entries)`` where each audit entry is
    ``{error_class, before, after, matched}``.  Entries whose ``quote`` was
    never found get ``matched=False`` and the correction is skipped; the
    caller should log them as "unmatched" warnings.

    The caller (``run_review_gate``) wraps this in a try/except so any
    unexpected failure degrades safely to the original summary.
    """
    audit_entries: list[dict] = []
    # Work entirely in NFKC-normalised space so fullwidth/halfwidth and
    # NFC/NFD differences don't cause false match failures (D13).
    result = _nfkc(summary_raw)

    for corr in corrections:
        if not isinstance(corr, dict):
            continue  # skip non-dict entries (defensive, R6 / 🟢-3)
        quote_raw = corr.get("quote", "")
        replacement_raw = corr.get("replacement", "")
        context_raw = corr.get("context")
        error_class = corr.get("error_class", "unknown")

        if not quote_raw:
            continue

        quote = _nfkc(quote_raw)
        replacement = _nfkc(replacement_raw)
        context = _nfkc(context_raw) if context_raw else None

        # Check occurrence count in the normalised text
        n_occurrences = result.count(quote)
        matched = False

        if n_occurrences == 0:
            audit_entries.append(
                {
                    "error_class": error_class,
                    "before": quote_raw,
                    "after": replacement_raw,
                    "matched": False,
                }
            )
            continue

        # Context-anchored match (D3)
        if context and n_occurrences > 1:
            needle = context + quote
            idx = result.find(needle)
            if idx != -1:
                start = idx + len(context)
                result = result[:start] + replacement + result[start + len(quote) :]
                matched = True

        if not matched:
            result = result.replace(quote, replacement, 1)
            if n_occurrences > 1:
                logger.warning(
                    "review_gate ambiguous quote: %d occurrences in digest, replacing first. "
                    "Consider adding 'context' to the correction JSON for disambiguation.",
                    n_occurrences,
                )
            matched = True

        audit_entries.append(
            {
                "error_class": error_class,
                "before": quote_raw,
                "after": replacement_raw,
                "matched": True,
            }
        )

    return result, audit_entries


def _emit_review_correction(
    intent_id: int,
    run_at: str,
    error_class: str,
    before: str,
    after: str,
) -> None:
    """Single audit sink for gate corrections (D6).

    Current implementation: WARNING log line with structured fields that can be
    grepped with ``grep review_gate_audit``.  Future B replaces this one
    function body with a DB write — no caller changes needed."""
    logger.warning(
        "review_gate_audit intent_id=%d run_at=%s class=%s before=%r after=%r",
        intent_id,
        run_at,
        error_class,
        before,
        after,
    )


# ---------------------------------------------------------------------------
# D2: build articles text from citations (for manual review endpoint)
# ---------------------------------------------------------------------------


async def build_articles_text_from_citations(
    citations: list[dict],
    body_fetcher: Callable[[str], Awaitable[str | None]],
    feed_name_map: dict[int, str],
) -> str | None:
    """Build an articles block from history citations for review-gate consumption.

    For each citation, fetches the article body via *body_fetcher* (strict
    mode: any body missing → returns None), then assembles a block matching
    the cron-path ``[N] title\\nbody\\nSource: ...`` format, with ``feed_name``
    inserted into the Source line (D6).

    *body_fetcher* receives the stripped-md5 (32-char hex, no dashes —
    D14).  *feed_name_map* is a pre-resolved ``{feed_id: feed_name}`` dict.

    Returns ``None`` when any article body cannot be retrieved (strict mode);
    otherwise returns the assembled articles text string.
    """
    entries: list[str] = []
    for i, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        article_id = citation.get("article_id", "")
        if not article_id:
            continue

        # D14: strip dashes from UUID-format article_id before Qdrant lookup.
        # get_article_detail (read_model.py:638) requires 32-char hex via
        # uuid.UUID(hex=md5), which rejects dashes.
        md5_hex = article_id.replace("-", "")

        body = await body_fetcher(md5_hex)
        if body is None:
            return None  # strict mode — one missing body aborts the whole review

        title = citation.get("title", "")
        url = citation.get("url", "")
        feed_id = citation.get("source")
        feed_name = feed_name_map.get(feed_id) if isinstance(feed_id, int) else None

        # D6: Source line with feed_name + url. Fallback rules:
        # (a) feed_name None/empty + url present → "Source: {url}"
        # (b) feed_name present + url empty → "Source: {feed_name}"
        # (c) feed_name None + url empty → "Source: (unknown)"
        # (d) both present → "Source: {feed_name} ({url})"
        if feed_name and url:
            source_line = f"Source: {feed_name} ({url})"
        elif feed_name:
            source_line = f"Source: {feed_name}"
        elif url:
            source_line = f"Source: {url}"
        else:
            source_line = "Source: (unknown)"

        entries.append(f"[{i}] {title}\n{body}\n{source_line}")

    return _ENTRY_SEPARATOR.join(entries) if entries else ""


# ---------------------------------------------------------------------------
# Main gate orchestrator
# ---------------------------------------------------------------------------


async def run_review_gate(
    llm: BaseLLMBackend,
    intent_id: int,
    summary_raw: str,
    articles_text: str,
    language: str,
    run_at: str,
    prompts_dir: str | None = None,
    history_text: str = "",
) -> tuple[str, list[dict]]:
    """Run the review gate over *summary_raw* using *llm*.

    Never raises — every failure path returns ``(summary_raw, [])`` unchanged
    (fail-open).  D1: returns ``(corrected_summary, corrections)`` where
    *corrections* is a list of ``{error_class, before, after, matched}``.

    *history_text* is the past summaries that were available when the digest
    was generated (formatted by ``format_history_text``).  The review LLM is
    told that claims carried over from history are NOT fabricated — this
    prevents false positives when a digest built on prior summaries references
    facts that aren't in the current batch of source articles.
    """
    from sembr.summarizer.templates import (  # noqa: PLC0415 (avoid cycle at module level)
        TemplateNotFoundError,
        TemplateRenderError,
        load_template,
        render_instruction_from_raw,
        render_system,
    )

    _prompts_dir = Path(prompts_dir) if prompts_dir else Path("/app/prompts")

    # 1. Render review templates
    try:
        review_system = render_system(_prompts_dir, _REVIEW_SYSTEM_TEMPLATE, language=language)
        raw_instruction = load_template(_prompts_dir, "instruction", _REVIEW_INSTRUCTION_TEMPLATE)
        review_user = render_instruction_from_raw(
            raw_instruction,
            intent_text=summary_raw,
            articles=articles_text,
            history=history_text,
        )
    except (TemplateNotFoundError, TemplateRenderError, FileNotFoundError) as exc:
        logger.warning(
            "review_gate template missing/unrenderable for intent_id=%d: %s; "
            "fail-open, returning original summary",
            intent_id,
            exc,
        )
        return summary_raw, []
    except Exception:
        logger.exception(
            "review_gate unexpected template error for intent_id=%d; fail-open",
            intent_id,
        )
        return summary_raw, []

    # 2. Budget check (D5)
    total_chars = len(review_system) + len(review_user)
    limit = int(llm.max_prompt_chars * _BUDGET_SAFETY_RATIO)
    if total_chars > limit:
        logger.warning(
            "review_gate budget exceeded for intent_id=%d: "
            "digest=%d system=%d total=%d limit=%d; "
            "fail-open, returning original summary",
            intent_id,
            len(summary_raw),
            len(review_system),
            total_chars,
            limit,
        )
        return summary_raw, []
    logger.debug(
        "review_gate budget ok for intent_id=%d: digest=%d system=%d total=%d limit=%d",
        intent_id,
        len(summary_raw),
        len(review_system),
        total_chars,
        limit,
    )

    # 3. Call review LLM
    try:
        raw_response = await llm.summarize(review_user, system=review_system)
    except Exception:
        logger.exception("review_gate LLM call failed for intent_id=%d; fail-open", intent_id)
        return summary_raw, []

    # 4. Parse JSON
    try:
        parsed = _parse_review_json(raw_response)
    except ValueError:
        logger.warning(
            "review_gate JSON parse failed for intent_id=%d; raw=%r; fail-open",
            intent_id,
            raw_response[:200],
        )
        return summary_raw, []

    corrections = parsed.get("corrections", [])
    if not isinstance(corrections, list):
        logger.warning(
            "review_gate 'corrections' is not a list for intent_id=%d; fail-open",
            intent_id,
        )
        return summary_raw, []

    # 5. Empty corrections → zero-touch
    if not corrections:
        logger.info(
            "review_gate zero corrections for intent_id=%d; raw (first 500 chars): %r",
            intent_id,
            raw_response[:500],
        )
        return summary_raw, []

    # 6. Apply corrections + audit (never-raise — any unexpected failure
    # in string ops or audit emission degrades to fail-open; R7).
    try:
        corrected, audit_entries = _apply_corrections(summary_raw, corrections)
    except Exception:
        logger.exception(
            "review_gate _apply_corrections failed for intent_id=%d; fail-open",
            intent_id,
        )
        return summary_raw, []

    n_matched = 0
    n_unmatched = 0
    try:
        for entry in audit_entries:
            if entry["matched"]:
                _emit_review_correction(
                    intent_id,
                    run_at,
                    entry["error_class"],
                    entry["before"],
                    entry["after"],
                )
                n_matched += 1
            else:
                logger.warning(
                    "review_gate unmatched quote intent_id=%d class=%s quote=%r",
                    intent_id,
                    entry["error_class"],
                    entry["before"][:120],
                )
                n_unmatched += 1

        if n_matched > 0 or n_unmatched > 0:
            logger.warning(
                "review_gate intent_id=%d corrections=%d matched=%d unmatched=%d",
                intent_id,
                len(corrections),
                n_matched,
                n_unmatched,
            )
    except Exception:
        logger.exception(
            "review_gate audit logging failed for intent_id=%d; "
            "corrections still applied, fail-open",
            intent_id,
        )

    # D1: return both corrected summary and corrections for callers that need
    # the diff (manual review endpoint).  Cron gate ignores the second element.
    corrections_list: list[dict] = [
        {
            "error_class": e["error_class"],
            "before": e["before"],
            "after": e["after"],
            "matched": e["matched"],
        }
        for e in audit_entries
    ]
    return corrected, corrections_list

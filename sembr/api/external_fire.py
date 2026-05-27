# SPDX-License-Identifier: Apache-2.0
"""POST /api/external/intents/{intent_id}/fire — synchronous fire endpoint.

Single-shot HTTP request/response form of the reverse-RAG digest, exposed for
external programs. Distinct from the internal async ``/intents/{id}/fire``:

  * synchronous — caller blocks until summary/error is ready (typically tens
    of seconds; long-poll clients should set a generous read timeout);
  * never writes ``match_seen`` and never invokes ``app.state.on_match`` (no
    notification side-effects);
  * shares the 1/intent/60s rate-limit bucket with the internal fire endpoint.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.matcher.fire_tasks import check_and_record_fire
from sembr.matcher.scan import ScanOptions, scan_once
from sembr.models import CronSchedule

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/external", tags=["external-fire"])

# Cap on summary_error length to prevent leaking traceback / paths / provider
# URLs from str(exc); the API contract is "short string, safe to log".
_SUMMARY_ERROR_MAX = 200

# Path-separator + whitespace scrub. httpx exceptions routinely include the
# upstream URL (``Read timeout for https://api.../v1/chat``) and
# TemplateNotFoundError carries the on-disk path (``/app/prompts/...``) —
# external callers should never observe these. Replace with a single space so
# the type-name + remaining context still reads like a sentence.
_SCRUB_RE = re.compile(r"[/\\\r\n\t]+")


class ExternalFireRequest(BaseModel):
    """Body for POST /api/external/intents/{intent_id}/fire.

    All fields optional — when omitted, falls back to the intent's stored
    value. ``feed_ids`` honours the same ``None=all / []=none / [1,3]=subset``
    semantics as ``Intent.feed_filter.ids``; an explicit ``[]`` matches nothing
    and short-circuits ``scan_once`` before any Qdrant call.
    """

    model_config = ConfigDict(extra="forbid")

    lookback_seconds: int | None = Field(default=None, ge=300, le=2592000)
    threshold: float | None = Field(default=None, ge=0.20, le=0.95)
    skip_seen: bool | None = None
    feed_ids: list[int] | None = None
    persist: bool = False


class ExternalFireMatch(BaseModel):
    article_id: str
    score: float
    title: str
    url: str
    published_at: str | None = None
    feed_id: int | None = None


class ExternalFireResponse(BaseModel):
    intent_id: int
    match_count: int
    matches: list[ExternalFireMatch]
    summary: str | None
    summary_error: str | None


def _match_to_payload(m: Any) -> ExternalFireMatch:
    payload = m.payload or {}
    return ExternalFireMatch(
        article_id=m.article_id,
        score=m.score,
        title=payload.get("title", ""),
        url=payload.get("url", ""),
        published_at=payload.get("published_at"),
        feed_id=payload.get("feed_id"),
    )


def _format_summary_error(exc: BaseException) -> str:
    """Format an external-facing error string: ``"<ExcType>: <scrubbed-str(exc)[:200]>"``.

    Bounded length, no traceback, and any ``/`` ``\\`` newline / tab is
    collapsed to a space so URLs / file paths / multi-line LLM provider
    responses cannot be observed by external callers. Scrub before truncation
    so a path that straddles the 200-char boundary still gets fully removed.
    """
    msg = _SCRUB_RE.sub(" ", str(exc))[:_SUMMARY_ERROR_MAX]
    return f"{type(exc).__name__}: {msg}"


@router.post(
    "/intents/{intent_id}/fire",
    response_model=ExternalFireResponse,
    response_model_exclude_none=False,
)
async def post_external_fire(
    intent_id: int,
    body: ExternalFireRequest,
    request: Request,
) -> ExternalFireResponse:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    if not isinstance(intent.schedule, CronSchedule):
        # Distinct wording from /intents/{id}/fire so 409 logs make the
        # endpoint of origin obvious.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "POST /api/external/.../fire is only valid for cron-mode intents; "
                "event-mode intents have no lookback semantics."
            ),
        )

    if not check_and_record_fire(intent_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="fire rate limit: 1 request per intent per 60s",
        )

    options = ScanOptions(
        lookback_seconds=body.lookback_seconds
        if body.lookback_seconds is not None
        else intent.schedule.lookback_seconds,
        threshold=body.threshold if body.threshold is not None else intent.threshold,
        skip_seen=body.skip_seen if body.skip_seen is not None else intent.schedule.skip_seen,
        feed_ids=(
            body.feed_ids
            if body.feed_ids is not None
            else (intent.feed_filter.ids if intent.feed_filter else None)
        ),
        write_match_seen=False,  # external fire never writes match_seen
        propagate_qdrant_errors=True,  # distinguish "0 hits" from "qdrant down"
    )

    qdrant_client = request.app.state.qdrant.client
    try:
        matches = await scan_once(intent, options, conn, qdrant_client)
    except Exception:
        # Hide internal exception detail from external callers.
        logger.exception("external_fire intent_id=%d scan_once Qdrant failure", intent_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="qdrant query failed",
        )

    # 0 hits → skip LLM entirely; summary stays None.
    if not matches:
        return ExternalFireResponse(
            intent_id=intent_id,
            match_count=0,
            matches=[],
            summary=None,
            summary_error=None,
        )

    pipeline = getattr(request.app.state, "summary_pipeline", None)
    if pipeline is None:
        # Defensive: lifespan should always wire this. AttributeError-style 500
        # is preferable to a silent ``summary: null`` masquerading as success.
        logger.error("external_fire intent_id=%d: app.state.summary_pipeline is missing", intent_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="summary pipeline unavailable",
        )

    summary: str | None
    summary_error: str | None
    try:
        result = await pipeline.compute_summary(matches)
    except Exception as exc:
        # Template + LLM errors all map to summary_error; we don't leak the
        # distinction to external callers. Server-side log uses
        # ``logger.exception`` so the traceback (which is *not* exposed to
        # the client) is available for debugging upstream LLM failures —
        # otherwise diagnosing a SiliconFlow / DeepSeek outage from the
        # scrubbed one-liner alone is impossible.
        summary = None
        summary_error = _format_summary_error(exc)
        logger.exception(
            "external_fire intent_id=%d compute_summary failed: %s",
            intent_id,
            summary_error,
        )
    else:
        # result is None for skip-class conditions (empty intent_text /
        # body_budget deficit / ctx fetch failed). Surfaced as summary=null,
        # summary_error=null — same as 0 hits but with matches present.
        summary = result.summary if result is not None else None
        summary_error = None
        if body.persist and result is not None:
            from sembr.db.summary_history import save_summary  # noqa: PLC0415

            await save_summary(get_conn(), result)

    return ExternalFireResponse(
        intent_id=intent_id,
        match_count=len(matches),
        matches=[_match_to_payload(m) for m in matches],
        summary=summary,
        summary_error=summary_error,
    )

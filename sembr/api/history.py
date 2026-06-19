# SPDX-License-Identifier: Apache-2.0
"""History endpoints — list / delete / backfill / aggregate the persisted cron summaries.

Path prefix is ``/intents/{intent_id}/history*`` + ``/intents/{intent_id}/backfill*``
to match the existing ``/intents/{intent_id}/fire`` shape (no DashboardToken
gate today; hardening lives at the reverse proxy / DASHBOARD_TOKEN layer).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from sembr.db.feeds import get_feed_names as db_get_feed_names
from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.db.summary_history import (
    delete_summary,
    get_summary_by_id,
    list_summaries,
    list_summaries_between,
    update_summary,
)
from sembr.matcher.backfill import probe_oldest_news_ts, run_backfill
from sembr.matcher.backfill_tasks import (
    create_task,
    get_task,
    release_intent,
    try_acquire_intent,
)
from sembr.matcher.cron_recall import past_n_fire_times
from sembr.models import CronSchedule
from sembr.summarizer.aggregate import MissingPlaceholderError, aggregate_history
from sembr.summarizer.llm.base import LLMError
from sembr.summarizer.models import Citation, SummaryResult

logger = logging.getLogger(__name__)

router = APIRouter(tags=["intents"])

# Strong refs for in-flight backfill background tasks; mirrors fire.py pattern.
_BG_TASKS: set[asyncio.Task] = set()


class BackfillRequest(BaseModel):
    past_runs: int = Field(ge=1, le=365)


class PatchHistoryRequest(BaseModel):
    summary: str = Field(min_length=1, max_length=100_000)


class ReviewResponse(BaseModel):
    original: str
    corrected: str
    corrections: list[dict]
    gate_diagnostics: str = ""  # "ok" = LLM ran; "no_changes" = clean; "fail_open" = gate errored


def _task_to_payload(task) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "intent_id": task.intent_id,
        "status": task.status,
        "started_at": task.started_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "progress": {
            "done": task.progress.done,
            "skipped": task.progress.skipped,
            "empty_runs": task.progress.empty_runs,
            "total": task.progress.total,
        },
        "error_reason": task.error_reason,
    }


# ---------------------------------------------------------------------------
# GET /intents/{id}/history
# ---------------------------------------------------------------------------


@router.get("/intents/{intent_id}/history")
async def get_history(
    intent_id: int,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    rows = await list_summaries(conn, intent_id, limit=limit, offset=offset)
    return {"intent_id": intent_id, "limit": limit, "offset": offset, "rows": rows}


# ---------------------------------------------------------------------------
# DELETE /intents/{id}/history/{row_id}
# ---------------------------------------------------------------------------


@router.delete(
    "/intents/{intent_id}/history/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def delete_history_row(intent_id: int, row_id: int):
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    deleted = await delete_summary(conn, intent_id, row_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="history row not found")


# ---------------------------------------------------------------------------
# POST /intents/{id}/history/{row_id}/review
# ---------------------------------------------------------------------------


@router.post(
    "/intents/{intent_id}/history/{row_id}/review",
    response_model=ReviewResponse,
)
async def review_history_row(intent_id: int, row_id: int, request: Request) -> dict[str, Any]:
    """Run the review gate over a persisted history row and return the diff.

    Returns the original summary, corrected summary, and the list of
    corrections so the UI can render a before/after comparison.
    """
    from sembr.dashboard.read_model import get_article_detail
    from sembr.summarizer.review import (
        build_articles_text_from_citations,
        run_review_gate,
    )

    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    # Fetch the specific history row by primary key (single-row query, no full scan)
    target_row = await get_summary_by_id(conn, intent_id, row_id)
    if target_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="history row not found")

    summary_raw: str = target_row["summary"]
    citations: list[dict] = target_row.get("citations", [])

    if not summary_raw.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_summary", "message": "Digest is empty, nothing to review"},
        )

    if not citations:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "no_citations", "message": "No citations to check against"},
        )

    # Resolve feed names for source attribution checking (D6)
    feed_ids: list[int] = []
    for c in citations:
        if isinstance(c, dict) and isinstance(c.get("source"), int):
            feed_ids.append(c["source"])
    feed_name_map = await db_get_feed_names(conn, list(set(feed_ids)))

    # D14: body_fetcher strips dashes from UUID-format article_id before
    # Qdrant lookup, matching get_article_detail's uuid.UUID(hex=md5) contract.
    qclient = request.app.state.qdrant.client

    async def _fetch_body(md5_hex: str) -> str | None:
        detail = await get_article_detail(conn, qclient, md5_hex, "qdrant")
        if detail is None:
            return None
        return detail.body or ""  # empty body != expired (D6-c)

    # D2: build articles_text for review gate consumption
    articles_text = await build_articles_text_from_citations(
        citations, _fetch_body, feed_name_map
    )
    if articles_text is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "source_articles_expired",
                "message": "One or more source articles are no longer available in Qdrant",
            },
        )

    # --- Pre-flight: verify review templates exist on disk (fast, no DB/Qdrant) ---
    # Without these, run_review_gate will fail-open instantly and the user sees a
    # misleading "Review passed" toast.  Fail early with a clear error instead.
    from sembr.summarizer.templates import (  # noqa: PLC0415
        load_template as _load_tpl,
        render_instruction_from_raw as _render_inst,
        render_system as _render_sys,
    )
    from sembr.summarizer.review import _BUDGET_SAFETY_RATIO  # noqa: PLC0415

    _prompts_dir = Path("/app/prompts")
    try:
        review_system = _render_sys(_prompts_dir, "review", language=intent.language)
        raw_instruction = _load_tpl(_prompts_dir, "instruction", "review")
        review_user = _render_inst(
            raw_instruction,
            intent_text=summary_raw,
            articles=articles_text,
        )
    except FileNotFoundError as missing_tpl:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "review_templates_missing",
                "message": (
                    f"Review template not found: {missing_tpl}. "
                    "Ensure prompts/system/review.md and prompts/instruction/review.md "
                    "exist in the prompts directory."
                ),
            },
        ) from missing_tpl
    except Exception as tpl_exc:
        # Template render errors (syntax, bad placeholders) — surface to operator
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "review_template_error",
                "message": f"Failed to render review template: {tpl_exc}",
            },
        ) from tpl_exc

    # --- Budget check (D13): refuse early if digest+articles too long ---
    llm = request.app.state.llm_backend
    total_chars = len(review_system) + len(review_user)
    limit = int(llm.max_prompt_chars * _BUDGET_SAFETY_RATIO)
    if total_chars > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "digest_too_long",
                "message": (
                    f"Digest is too long to review ({total_chars} chars, "
                    f"budget {limit}). Consider reviewing a shorter digest."
                ),
            },
        )

    # Run the review gate
    run_at = target_row.get("run_at", "")
    corrected, corrections = await run_review_gate(
        llm,
        intent_id,
        summary_raw,
        articles_text,
        intent.language,
        run_at,
    )

    # Distinguish "LLM found nothing" from "gate failed silently"
    gate_diagnostics = "ok"
    if corrected == summary_raw and not corrections:
        # Could be "no issues found" OR "gate errored and returned original".
        # The gate logs on fail-open, so operators can check docker logs.
        gate_diagnostics = "no_changes_or_fail_open"

    return {
        "original": summary_raw,
        "corrected": corrected,
        "corrections": corrections,
        "gate_diagnostics": gate_diagnostics,
    }


# ---------------------------------------------------------------------------
# PATCH /intents/{id}/history/{row_id}
# ---------------------------------------------------------------------------


@router.patch(
    "/intents/{intent_id}/history/{row_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
async def patch_history_row(intent_id: int, row_id: int, body: PatchHistoryRequest):
    """Replace the summary field of a history row (D4).

    Note: only ``summary`` is updated — ``citations`` and ``run_at`` are left
    untouched.  The corrected summary may reference article numbers that are
    absent from the original citations array (if the review gate removed or
    renumbered citations).  Use DELETE + backfill to fully regenerate a row
    with synchronized citations.
    """
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    updated = await update_summary(conn, intent_id, row_id, body.summary)
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="history row not found")


# ---------------------------------------------------------------------------
# POST /intents/{id}/backfill
# ---------------------------------------------------------------------------


@router.post(
    "/intents/{intent_id}/backfill",
    status_code=status.HTTP_202_ACCEPTED,
)
async def post_backfill(
    intent_id: int,
    body: BackfillRequest,
    request: Request,
) -> dict[str, Any]:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    if not isinstance(intent.schedule, CronSchedule):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="backfill is only valid for cron-mode intents",
        )

    # Cheap pre-check: Qdrant depth.  Surfaces 422 to UI before we take the
    # lock or spawn the bg task; user fixes input (smaller N) and retries.
    fire_times = past_n_fire_times(intent.schedule, intent.timezone, body.past_runs)
    if fire_times:
        qdrant_client = request.app.state.qdrant.client
        oldest_news_ts = await probe_oldest_news_ts(qdrant_client)
        earliest_target = fire_times[-1]  # newest-first list → last element is oldest
        if oldest_news_ts is not None and earliest_target.timestamp() < oldest_news_ts:
            # Compute the max N that fits inside Qdrant coverage.
            oldest_dt = datetime.fromtimestamp(oldest_news_ts, tz=UTC)
            max_n = sum(1 for ft in fire_times if ft.timestamp() >= oldest_news_ts)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "qdrant_depth_insufficient",
                    "oldest_date": oldest_dt.strftime("%Y-%m-%d"),
                    "max_backfillable_runs": max_n,
                },
            )

    # Concurrency gate: atomic synchronous try-acquire.  See
    # backfill_tasks.try_acquire_intent for the no-TOCTOU rationale.
    if not try_acquire_intent(intent_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="backfill_in_progress")

    # From here on the lock is OURS; orchestrator's finally block releases.
    # Wrap acquire->spawn in BaseException try so a failure between (e.g.
    # MemoryError on create_task) doesn't leak the lock until process restart.
    try:
        task = create_task(intent_id=intent_id, total=body.past_runs)
        bg = asyncio.create_task(run_backfill(intent_id, body.past_runs, request.app, task))
        _BG_TASKS.add(bg)
        bg.add_done_callback(_BG_TASKS.discard)
    except BaseException:
        release_intent(intent_id)
        raise

    return {
        "task_id": task.task_id,
        "status_url": f"/intents/{intent_id}/backfill/{task.task_id}",
    }


# ---------------------------------------------------------------------------
# GET /intents/{id}/backfill/{task_id}
# ---------------------------------------------------------------------------


@router.get("/intents/{intent_id}/backfill/{task_id}")
async def get_backfill_status(intent_id: int, task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if task is None or task.intent_id != intent_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return _task_to_payload(task)


# ---------------------------------------------------------------------------
# Aggregate / Send / Export — request models & helpers
# ---------------------------------------------------------------------------

_MAX_RANGE_DAYS = 365


class AggregateRequest(BaseModel):
    since: str = Field(description="YYYY-MM-DD start date (inclusive)")
    until: str = Field(description="YYYY-MM-DD end date (inclusive)")
    prompt: str = Field(min_length=1, max_length=32_000)


class AggregateSendRequest(BaseModel):
    since: str = Field(description="YYYY-MM-DD start date (inclusive)")
    until: str = Field(description="YYYY-MM-DD end date (inclusive)")
    markdown: str = Field(min_length=1, max_length=100_000)


def _parse_date_range_in_tz(since_str: str, until_str: str, tz_name: str) -> tuple[str, str]:
    """Convert YYYY-MM-DD date strings to UTC ISO range in the intent's timezone.

    Returns ``(since_utc_iso, until_utc_iso)`` where *since* is midnight
    (00:00:00) and *until* is end-of-day (23:59:59) in the given timezone,
    both converted to UTC.
    """
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown timezone: {tz_name!r}",
        ) from None

    try:
        since_dt_naive = datetime.strptime(since_str, "%Y-%m-%d")
        until_dt_naive = datetime.strptime(until_str, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid date format, expected YYYY-MM-DD: {exc}",
        ) from exc

    if since_dt_naive > until_dt_naive:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="since must be <= until",
        )

    delta = (until_dt_naive - since_dt_naive).days
    if delta > _MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"date range exceeds {_MAX_RANGE_DAYS} day limit",
        )

    since_local = since_dt_naive.replace(tzinfo=tz)  # midnight
    until_local = until_dt_naive.replace(hour=23, minute=59, second=59, tzinfo=tz)

    since_utc = since_local.astimezone(UTC)
    until_utc = until_local.astimezone(UTC)

    return since_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), until_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _merge_citations(rows: list[dict]) -> list[Citation]:
    """Merge citations from *rows* (newest first), dedup by ``article_id`` keep-first, cap 50."""
    seen: set[str] = set()
    merged: list[Citation] = []
    for row in rows:
        for c_dict in row.get("citations", []):
            if not isinstance(c_dict, dict):
                continue
            aid = c_dict.get("article_id")
            if not aid or aid in seen:
                continue
            seen.add(aid)
            merged.append(Citation(**c_dict))
            if len(merged) >= 50:
                return merged
    return merged


# ---------------------------------------------------------------------------
# POST /intents/{id}/history/aggregate
# ---------------------------------------------------------------------------


@router.post("/intents/{intent_id}/history/aggregate")
async def post_aggregate(
    intent_id: int,
    body: AggregateRequest,
    request: Request,
) -> dict[str, Any]:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    since_utc, until_utc = _parse_date_range_in_tz(body.since, body.until, intent.timezone)
    rows = await list_summaries_between(conn, intent_id, since_utc, until_utc)

    if len(rows) == 0:
        return {"summary": None, "rows_total": 0, "rows_used": 0, "rows_dropped": 0}

    llm = request.app.state.llm_backend
    try:
        result = await aggregate_history(llm, body.prompt, rows)
    except MissingPlaceholderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except LLMError as exc:
        safe = str(exc)[:200]
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=safe) from exc

    return {
        "summary": result.summary,
        "rows_total": result.rows_total,
        "rows_used": result.rows_used,
        "rows_dropped": result.rows_dropped,
    }


# ---------------------------------------------------------------------------
# POST /intents/{id}/history/aggregate/send
# ---------------------------------------------------------------------------


@router.post("/intents/{intent_id}/history/aggregate/send")
async def post_aggregate_send(
    intent_id: int,
    body: AggregateSendRequest,
    request: Request,
) -> dict[str, Any]:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    from sembr.notifier.email import EmailChannelConfig

    if not any(isinstance(ch, EmailChannelConfig) for ch in intent.channels):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="intent has no channels configured",
        )

    _since_utc, _until_utc = _parse_date_range_in_tz(body.since, body.until, intent.timezone)
    result = SummaryResult(intent_id=intent_id, summary=body.markdown, citations=[])

    subject = f"[Sembr] {intent.name} — {body.since} ~ {body.until}"

    from sembr.notifier.dispatcher import dispatch_summary

    email_ch = request.app.state.email_channel
    outcomes = await dispatch_summary(conn, email_ch, result, strict=True, subject=subject)

    all_failed = all(not o.ok for o in outcomes)
    http_status = status.HTTP_502_BAD_GATEWAY if outcomes and all_failed else status.HTTP_200_OK

    return JSONResponse(
        status_code=http_status,
        content={
            "results": [{"type": o.type, "ok": o.ok, "error": o.error} for o in outcomes],
        },
    )


# ---------------------------------------------------------------------------
# GET /intents/{id}/history/export
# ---------------------------------------------------------------------------


@router.get("/intents/{intent_id}/history/export")
async def get_export(
    intent_id: int,
    since: str = Query(description="YYYY-MM-DD start date (inclusive)"),
    until: str = Query(description="YYYY-MM-DD end date (inclusive)"),
) -> JSONResponse:
    conn = get_conn()
    intent = await get_intent(conn, intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    since_utc, until_utc = _parse_date_range_in_tz(since, until, intent.timezone)
    rows = await list_summaries_between(conn, intent_id, since_utc, until_utc)

    filename = f"intent-{intent_id}-{since}-{until}.json"
    pretty = json.dumps(rows, indent=2, ensure_ascii=False)
    return Response(
        content=pretty,
        media_type="application/json",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )

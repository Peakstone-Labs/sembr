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
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.db.summary_history import delete_summary, list_summaries, list_summaries_between
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

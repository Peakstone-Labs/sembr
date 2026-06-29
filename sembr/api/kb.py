# SPDX-License-Identifier: Apache-2.0
"""Per-intent KB endpoints (delta-label/kb SF1, design §6 — standalone /api/kb).

A standalone router keyed by ``intent_id`` (KB is 1:1 per-intent), distinct from
the shared flat ``/api/prompts`` namespace (design §6.4). Paths are written in
full as ``/api/kb/...`` so they sit under the auth-gated ``/api/kb/`` prefix
(added to dashboard/auth.py) — an unauthenticated call gets 401 JSON.

- GET    /api/kb/{intent_id}/{kind}    — read events.md (+ content_hash for the PUT lock)
- PUT    /api/kb/{intent_id}/{kind}    — overwrite (optimistic-locked, size-capped)
- POST   /api/kb/{intent_id}/rebuild   — cold-start distill (confirm to overwrite, O3)
- POST   /api/kb/{intent_id}/lint      — manual lint (O2)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from sembr.db.intents import get_intent
from sembr.db.sqlite import get_conn
from sembr.db.summary_history import format_history_text
from sembr.kb import KB_KINDS
from sembr.kb import lint as _lint
from sembr.kb import merge as _merge
from sembr.kb.distill import bootstrap_intent
from sembr.kb.store import MANUAL_LINT_IDENTITY, KbSizeError, KbStore
from sembr.models import Intent
from sembr.summarizer.llm.base import LLMError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["kb"])

# Mirrors the extraction-spec generate cap — a cold-start distill is one pro call.
_REBUILD_TIMEOUT_S = 600.0
# Default lookback for the rebuild distill when the intent's schedule sets none.
_DEFAULT_HISTORY_DAYS = 7


class KbPutRequest(BaseModel):
    content: str = Field(min_length=0)
    # Optimistic-lock token from a prior GET. None = unconditional write (e.g.
    # first create); a stale token → 409 (design §3.4 / F2).
    base_hash: str | None = None


class KbRebuildRequest(BaseModel):
    confirm: bool = False


def _store(request: Request) -> KbStore:
    return request.app.state.kb_store


def _check_kind(kind: str) -> None:
    if kind not in KB_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown KB kind {kind!r}; expected one of {list(KB_KINDS)}",
        )


async def _require_intent(intent_id: int) -> Intent:
    intent = await get_intent(get_conn(), intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")
    return intent


@router.get("/api/kb/{intent_id}/{kind}")
async def get_kb(intent_id: int, kind: str, request: Request) -> dict[str, Any]:
    _check_kind(kind)
    await _require_intent(intent_id)
    store = _store(request)
    content = store.read(intent_id, kind)
    path = store.path(intent_id, kind)
    return {
        "intent_id": intent_id,
        "kind": kind,
        "exists": content is not None,
        "content": content or "",
        "size_bytes": len((content or "").encode("utf-8")),
        "mtime": path.stat().st_mtime if path.exists() else None,
        "content_hash": store.content_hash(intent_id, kind),
    }


@router.put("/api/kb/{intent_id}/{kind}")
async def put_kb(intent_id: int, kind: str, body: KbPutRequest, request: Request) -> dict[str, Any]:
    _check_kind(kind)
    await _require_intent(intent_id)
    store = _store(request)
    # Optimistic concurrency: reject if the file changed since the client's GET.
    current = store.content_hash(intent_id, kind)
    if body.base_hash is not None and current is not None and body.base_hash != current:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="KB changed since you loaded it; refresh and retry",
        )
    try:
        new_hash = await store.write(
            intent_id,
            body.content,
            kind=kind,
            message=f"edit intent-{intent_id} {kind} via dashboard",
        )
    except KbSizeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    return {
        "ok": True,
        "git_hash": new_hash,
        "content_hash": store.content_hash(intent_id, kind),
        "warnings": KbStore.validate_key_integrity(body.content),
    }


@router.post("/api/kb/{intent_id}/rebuild")
async def rebuild_kb(
    intent_id: int, request: Request, body: KbRebuildRequest | None = None
) -> dict[str, Any]:
    body = body or KbRebuildRequest()
    intent = await _require_intent(intent_id)
    store = _store(request)
    # O3: overwriting an existing KB requires explicit confirmation.
    if store.read(intent_id) is not None and not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="KB already exists; pass confirm=true to overwrite",
        )
    days = getattr(intent.schedule, "history_days", None) or _DEFAULT_HISTORY_DAYS
    history = await format_history_text(get_conn(), intent_id, days, None)
    if not history or not history.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="no summary history to distill — run the intent first",
        )
    # In-flight guard (review 🟡-1): reject a concurrent rebuild so two requests
    # can't both fire a pro distill (the expensive, once-meant-to-be-rare action).
    if not store.try_begin_rebuild(intent_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="rebuild already in progress"
        )
    backend = request.app.state.llm_backend
    model = request.app.state.settings.effective_kb_distill_model
    try:
        content = await asyncio.wait_for(
            bootstrap_intent(store, intent_id, history, backend, model=model),
            timeout=_REBUILD_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail="distill timed out"
        ) from exc
    except LLMError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"distill failed: {exc}"
        ) from exc
    finally:
        store.end_rebuild(intent_id)
    return {
        "status": "rebuilt",
        "intent_id": intent_id,
        "events": len(_merge.parse_events(content)),
        "content_hash": store.content_hash(intent_id),
    }


@router.post("/api/kb/{intent_id}/lint")
async def lint_kb(intent_id: int, request: Request) -> dict[str, Any]:
    await _require_intent(intent_id)
    store = _store(request)
    stats = await _lint.run_for_intent(
        store,
        intent_id,
        identity=MANUAL_LINT_IDENTITY,
        backend=request.app.state.llm_backend,
        model=request.app.state.settings.effective_kb_merge_model,
    )
    if stats.skipped == "not_bootstrapped":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="KB not built yet; rebuild first",
        )
    return {
        "merged_dups": stats.merged_dups,
        "merged_near_dup": stats.merged_near_dup,
        "archived": stats.archived,
        "marked": stats.marked,
    }

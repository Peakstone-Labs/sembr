"""POST/GET/PUT/DELETE /intents router."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from sembr.db.intents import (
    create_intent,
    delete_intent,
    get_intent,
    list_intents,
    update_intent,
    update_intent_raw,
)
from sembr.db.sqlite import get_conn
from sembr.models import Intent, IntentCreate, IntentUpdate
from sembr.vector_store.intents import (
    delete_intent_point,
    update_intent_payload,
    upsert_intent_point,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intents", tags=["intents"])


def _build_payload(intent: Intent, model_version: str) -> dict[str, Any]:
    """D10 / D12: payload fields required by matcher; channels and name omitted (D14)."""
    return {
        "intent_id": intent.id,
        "text": intent.text,
        "threshold": intent.threshold,
        "enabled": intent.enabled,
        "tags": intent.tags,
        "embedding_model_version": model_version,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }


@router.post("", response_model=Intent, status_code=status.HTTP_201_CREATED)
async def post_intent(body: IntentCreate, request: Request) -> Intent:
    embedder = request.app.state.embedder
    if not embedder.is_loaded:  # D5: fast-fail before any DB write
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready")

    conn = get_conn()
    intent = await create_intent(conn, body)  # D1: SQLite first

    try:
        [vector] = await embedder.aembed([body.text])  # D1: embed second
        await upsert_intent_point(  # D1: Qdrant third
            request.app.state.qdrant.client,
            intent.id,
            vector,
            payload=_build_payload(intent, embedder.model_version),
        )
    except Exception as exc:
        try:
            deleted = await delete_intent(conn, intent.id)
            if not deleted:  # M4: log if the row was already gone before rollback
                logger.warning("POST rollback no-op for intent_id=%d: row already absent", intent.id)
        except Exception as rollback_exc:
            logger.error("POST rollback failed for intent_id=%d: %s", intent.id, rollback_exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist intent vector",
        ) from exc

    return intent


@router.get("", response_model=list[Intent])
async def get_intents() -> list[Intent]:
    return await list_intents(get_conn())


@router.get("/{intent_id}", response_model=Intent)
async def get_intent_by_id(intent_id: int) -> Intent:
    intent = await get_intent(get_conn(), intent_id)
    if intent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")
    return intent


@router.put("/{intent_id}", response_model=Intent)
async def put_intent(intent_id: int, body: IntentUpdate, request: Request) -> Intent:
    conn = get_conn()
    current = await get_intent(conn, intent_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    text_changed = body.text is not None and body.text.strip() != current.text.strip()  # D6
    embedder = request.app.state.embedder
    if text_changed and not embedder.is_loaded:  # D5: only gate when re-embed needed
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready")

    updated = await update_intent(conn, intent_id, body)  # D1: SQLite first

    qdrant_client = request.app.state.qdrant.client
    qdrant_written = False
    try:
        if text_changed:
            [vector] = await embedder.aembed([updated.text])
            await upsert_intent_point(
                qdrant_client,
                intent_id,
                vector,
                payload=_build_payload(updated, embedder.model_version),
            )
            qdrant_written = True
        else:
            await update_intent_payload(  # D7: payload-only sync (enabled toggle, threshold, etc.)
                qdrant_client,
                intent_id,
                payload=_build_payload(updated, embedder.model_version),
            )
    except Exception as exc:
        # SQLite rollback runs first so log messages below reflect true state (L2-I1)
        sqlite_state = "rolled-back"
        try:
            await update_intent_raw(conn, intent_id, current)
        except Exception as rb:
            sqlite_state = "rollback-failed"
            logger.error("PUT SQLite rollback failed for intent_id=%d: %s", intent_id, rb)

        # I2: if the new vector was already written server-side, attempt best-effort revert
        if text_changed and qdrant_written:
            try:
                await delete_intent_point(qdrant_client, intent_id)
                logger.error(
                    "PUT inconsistency: intent_id=%d sqlite=%s qdrant=best-effort-deleted",
                    intent_id,
                    sqlite_state,
                )
            except Exception as revert_exc:
                logger.error(
                    "PUT inconsistency: intent_id=%d sqlite=%s qdrant=uncertain (%s)",
                    intent_id,
                    sqlite_state,
                    revert_exc,
                )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to sync intent vector",
        ) from exc

    return updated


@router.delete("/{intent_id}")
async def delete_intent_handler(intent_id: int, request: Request) -> Response:
    conn = get_conn()
    current = await get_intent(conn, intent_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    qdrant_client = request.app.state.qdrant.client
    try:
        # D8: Qdrant deleted first so matcher stops immediately (safer than GET-visible orphan)
        await delete_intent_point(qdrant_client, intent_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to delete intent vector",
        ) from exc

    try:
        await delete_intent(conn, intent_id)
    except Exception as exc:
        # I3: Qdrant is already deleted; log orphan SQLite row so operators can reconcile
        logger.error(
            "DELETE inconsistency: intent_id=%d qdrant=deleted sqlite=failed (%s) — "
            "row remains visible via GET but matcher will not consume it (no Qdrant vector)",
            intent_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="intent vector deleted but database record removal failed",
        ) from exc

    return Response(status_code=status.HTTP_204_NO_CONTENT)

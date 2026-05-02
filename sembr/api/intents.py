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
from sembr.db.match_seen import clear_intent
from sembr.db.sqlite import get_conn
from sembr.matcher.jobs import (
    register_intent_job,
    reregister_intent_job,
    unregister_intent_job,
)
from sembr.models import Intent, IntentCreate, IntentUpdate
from sembr.summarizer.templates import template_exists
from sembr.vector_store.intents import (
    delete_intent_point,
    update_intent_payload,
    upsert_intent_point,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intents", tags=["intents"])


def _validate_templates(request: Request, system_tpl: str, instruction_tpl: str) -> None:
    """Raise 422 if either named template file does not exist on disk."""
    prompts_dir = request.app.state.settings.prompts_dir
    for kind, name in (("system", system_tpl), ("instruction", instruction_tpl)):
        if not template_exists(prompts_dir, kind, name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"field": f"{kind}_template", "value": name, "reason": "template not found"},
            )


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
    _validate_templates(request, body.system_template, body.instruction_template)

    conn = get_conn()
    intent = await create_intent(conn, body)  # D1: SQLite first

    qdrant_client = request.app.state.qdrant.client
    qdrant_written = False
    try:
        [vector] = await embedder.aembed([body.text])  # D1: embed second
        await upsert_intent_point(  # D1: Qdrant third
            qdrant_client,
            intent.id,
            vector,
            payload=_build_payload(intent, embedder.model_version),
        )
        qdrant_written = True
        # D10: sync event-mode intent into in-process cache (after Qdrant, before job reg)
        from sembr.models import EventSchedule  # noqa: PLC0415
        if body.enabled and isinstance(intent.schedule, EventSchedule):
            from sembr.matcher.event_cache import EventIntentEntry  # noqa: PLC0415
            request.app.state.event_intent_cache.add(
                intent.id,
                EventIntentEntry(
                    vector=vector,
                    threshold=intent.threshold,
                    feed_filter_ids=intent.feed_filter.ids if intent.feed_filter else None,
                    schedule=intent.schedule,  # type: ignore[arg-type]
                ),
            )
        # D8: register_job last; failure rolls back Qdrant + SQLite (no-op for event-mode)
        if body.enabled:
            register_intent_job(request.app.state.scheduler, intent, request.app, fire_immediately=True)
    except Exception as exc:
        # Rollback in reverse order: cache, job (already failed/not-registered), Qdrant, SQLite
        from sembr.models import EventSchedule  # noqa: PLC0415
        if isinstance(intent.schedule, EventSchedule):
            request.app.state.event_intent_cache.remove(intent.id)
        if qdrant_written:
            try:
                await delete_intent_point(qdrant_client, intent.id)
            except Exception as del_exc:
                logger.error(
                    "POST Qdrant rollback failed for intent_id=%d: %s", intent.id, del_exc
                )
        try:
            deleted = await delete_intent(conn, intent.id)
            if not deleted:  # M4: log if the row was already gone before rollback
                logger.warning("POST rollback no-op for intent_id=%d: row already absent", intent.id)
        except Exception as rollback_exc:
            logger.error("POST SQLite rollback failed for intent_id=%d: %s", intent.id, rollback_exc)
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
    enabled_changed = body.enabled is not None and body.enabled != current.enabled
    schedule_changed = (
        (body.schedule is not None and body.schedule != current.schedule)
        or (body.timezone is not None and body.timezone != current.timezone)
    )

    embedder = request.app.state.embedder
    if text_changed and not embedder.is_loaded:  # D5: only gate when re-embed needed
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready")
    # Validate template existence only when the field is actually being changed.
    effective_system = body.system_template if body.system_template is not None else current.system_template
    effective_instruction = body.instruction_template if body.instruction_template is not None else current.instruction_template
    if body.system_template is not None or body.instruction_template is not None:
        _validate_templates(request, effective_system, effective_instruction)

    # D16: schedule.mode is immutable — mode change requires delete + recreate
    if body.schedule is not None and body.schedule.mode != current.schedule.mode:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="schedule.mode is immutable; delete and recreate the intent to change mode",
        )

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

    # D4: clear match_seen when text changes and intent is/becomes enabled.
    # Outside the scheduler try/except because this is a DB write, not a best-effort job sync.
    # Failure → 500 so the caller can retry; silently returning 200 with stale dedup rows
    # would suppress legitimate new matches against the re-embedded vector.
    if text_changed and updated.enabled:
        try:
            await clear_intent(conn, intent_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to clear match deduplication state",
            ) from exc

    # D10: sync event-mode intent into in-process cache.
    # Placed after Qdrant confirm and clear_intent; outside the Qdrant try/except so no
    # cache rollback is needed (if we get here, all writes succeeded).
    from sembr.models import EventSchedule  # noqa: PLC0415
    if isinstance(updated.schedule, EventSchedule):
        from sembr.matcher.event_cache import EventIntentEntry  # noqa: PLC0415
        cache = request.app.state.event_intent_cache
        if updated.enabled:
            # Resolve vector: new embed if text changed, else reuse cached or retrieve from Qdrant
            if text_changed:
                cache_vector = vector  # from upsert above
            else:
                existing_entry = cache.get(intent_id)
                if existing_entry is not None:
                    cache_vector = existing_entry.vector
                else:
                    # Intent was disabled (not in cache); retrieve once from Qdrant
                    pts = await qdrant_client.retrieve(
                        collection_name="intents_current", ids=[intent_id], with_vectors=True
                    )
                    cache_vector = list(pts[0].vector) if pts and pts[0].vector else None
            if cache_vector is not None:
                cache.add(
                    intent_id,
                    EventIntentEntry(
                        vector=cache_vector,
                        threshold=updated.threshold,
                        feed_filter_ids=updated.feed_filter.ids if updated.feed_filter else None,
                        schedule=updated.schedule,  # type: ignore[arg-type]
                    ),
                )
        else:
            cache.remove(intent_id)

    # Matcher job lifecycle (D3/D4/D5). Best-effort: job sync failure is logged but
    # does not fail the request — register_all_enabled at restart recovers the state.
    scheduler = request.app.state.scheduler
    try:
        if enabled_changed:
            # D3: enable/disable takes precedence over job lifecycle
            if updated.enabled:
                register_intent_job(scheduler, updated, request.app, fire_immediately=True)
            else:
                unregister_intent_job(scheduler, intent_id)
        elif updated.enabled:
            if text_changed or schedule_changed:
                # D4/D5: text or schedule change → reregister with updated trigger
                reregister_intent_job(scheduler, updated, request.app)
    except Exception as exc:
        logger.warning(
            "matcher job sync failed for intent_id=%d: %s (recovers on restart)", intent_id, exc
        )

    return updated


@router.delete("/{intent_id}")
async def delete_intent_handler(intent_id: int, request: Request) -> Response:
    conn = get_conn()
    current = await get_intent(conn, intent_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    # D9: unregister job first so no new ticks fire during the delete window
    unregister_intent_job(request.app.state.scheduler, intent_id)
    # D10: remove from event cache (no-op for cron-mode intents)
    from sembr.models import EventSchedule  # noqa: PLC0415
    if isinstance(current.schedule, EventSchedule):
        request.app.state.event_intent_cache.remove(intent_id)

    qdrant_client = request.app.state.qdrant.client
    try:
        await delete_intent_point(qdrant_client, intent_id)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to delete intent vector",
        ) from exc

    try:
        # match_seen rows cascade automatically via ON DELETE CASCADE (D10)
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

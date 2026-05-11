"""POST/GET/PUT/DELETE /intents router."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from sembr.db.intent_sub_texts import (
    _replace_in_txn as _sub_texts_replace_in_txn,
)
from sembr.db.intents import (
    _update_intent_in_txn,
    _update_intent_raw_in_txn,
    create_intent,
    delete_intent,
    get_intent,
    list_intents,
    update_intent,
    update_intent_raw,
)
from sembr.db.match_seen import clear_intent
from sembr.db.sqlite import get_conn, transaction
from sembr.matcher.event_cache import EventIntentEntry
from sembr.matcher.jobs import (
    register_intent_job,
    reregister_intent_job,
    unregister_intent_job,
)
from sembr.models import EventSchedule, Intent, IntentCreate, IntentUpdate, SubTextSpec
from sembr.summarizer import templates as _templates
from sembr.summarizer.templates import template_exists
from sembr.vector_store.intents import ALIAS_NAME as _INTENTS_ALIAS
from sembr.vector_store.intents import (
    delete_intent_point,
    update_intent_payload,
    upsert_intent_point,
)
from sembr.vector_store.qdrant import extract_named_vector

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/intents", tags=["intents"])


def _validate_templates(system_tpl: str, instruction_tpl: str) -> None:
    """Raise 422 if either named template file does not exist on disk."""
    for kind, name in (("system", system_tpl), ("instruction", instruction_tpl)):
        if not template_exists(_templates.PROMPTS_DIR, kind, name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"field": f"{kind}_template", "value": name, "reason": "template not found"},
            )


def _build_payload(intent: Intent, model_version: str) -> dict[str, Any]:
    """D10 / D12: payload fields required by matcher; channels and name omitted (D14).

    intent-match-enhancement: adds `sub_text_slots` listing populated alt_* slot
    names so the dashboard can render "this intent has N sub-vectors" without
    re-reading Qdrant. Matcher does NOT depend on this field (it derives slots
    from the Qdrant point's vector dict itself), so payload drift is non-fatal.
    """
    slot_names = [f"alt_{i}" for i in range(len(intent.sub_texts))]
    return {
        "intent_id": intent.id,
        "text": intent.text,
        "threshold": intent.threshold,
        "enabled": intent.enabled,
        "tags": intent.tags,
        "embedding_model_version": model_version,
        "sub_text_slots": slot_names,
        "created_at": intent.created_at,
        "updated_at": intent.updated_at,
    }


def _slot_dict_from_vectors(vectors: list[list[float]]) -> dict[str, list[float]]:
    """[main_vec, alt0_vec, alt1_vec, ...] → {"main":..., "alt_0":..., "alt_1":..., ...}."""
    out: dict[str, list[float]] = {"main": vectors[0]}
    for i, v in enumerate(vectors[1:]):
        out[f"alt_{i}"] = v
    return out


def _slot_set(sub_text_count: int) -> set[str]:
    return {"main"} | {f"alt_{i}" for i in range(sub_text_count)}


def _diff_sub_texts(
    old: list[SubTextSpec], new: list[SubTextSpec]
) -> tuple[bool, bool, bool]:
    """R6 / D23: position-aligned diff. Returns (added, edited, deleted).

    `sub_text_label_only_changed` from the design's R6 truth table is implicit:
    it's exactly the case where this function returns (False, False, False) but
    new != old (only languages changed). The PUT handler never branches on it
    directly — it ends up in the payload-only-sync branch, which is the correct
    target per R6 ("纯 DB 路径"). Returning it as a bool was unused (review 🟢-2).
    """
    added = edited = deleted = False
    for i in range(max(len(old), len(new))):
        o = old[i] if i < len(old) else None
        n = new[i] if i < len(new) else None
        if o is None and n is not None:
            added = True
        elif o is not None and n is None:
            deleted = True
        elif o is not None and n is not None and o.text != n.text:
            edited = True
    return added, edited, deleted


@router.post("", response_model=Intent, status_code=status.HTTP_201_CREATED)
async def post_intent(body: IntentCreate, request: Request) -> Intent:
    embedder = request.app.state.embedder
    if not embedder.is_loaded:  # D5: fast-fail before any DB write
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready")
    _validate_templates(body.system_template, body.instruction_template)

    conn = get_conn()
    intent = await create_intent(conn, body)  # D1: SQLite first (intents row + sub_texts atomically)

    qdrant_client = request.app.state.qdrant.client
    qdrant_written = False
    try:
        texts_to_embed = [body.text] + [st.text for st in body.sub_texts]
        vectors = await embedder.aembed(texts_to_embed)  # batch ≤ 4 << 32 single-shot
        slot_vecs = _slot_dict_from_vectors(vectors)
        await upsert_intent_point(  # D1: Qdrant third
            qdrant_client,
            intent.id,
            slot_vecs,
            payload=_build_payload(intent, embedder.model_version),
        )
        qdrant_written = True
        # D10: sync event-mode intent into in-process cache (after Qdrant, before job reg)
        if body.enabled and isinstance(intent.schedule, EventSchedule):
            request.app.state.event_intent_cache.add(
                intent.id,
                EventIntentEntry(
                    vectors={k: list(v) for k, v in slot_vecs.items()},  # defensive copies
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
        # delete_intent CASCADES sub_texts via FK ON DELETE CASCADE — no separate cleanup needed.
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
            if not deleted:
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
    # D22 step 1: current snapshot (Intent has current.sub_texts populated by get_intent)
    current = await get_intent(conn, intent_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="intent not found")

    # D22 step 2: compute diff booleans against the snapshot (NOT against `updated.*`).
    text_changed = body.text is not None and body.text.strip() != current.text.strip()
    enabled_changed = body.enabled is not None and body.enabled != current.enabled
    schedule_changed = (
        (body.schedule is not None and body.schedule != current.schedule)
        or (body.timezone is not None and body.timezone != current.timezone)
    )

    if body.sub_texts is not None:
        sub_texts_added, sub_texts_edited, sub_texts_deleted = _diff_sub_texts(
            current.sub_texts, body.sub_texts
        )
    else:
        sub_texts_added = sub_texts_edited = sub_texts_deleted = False

    needs_reembed = text_changed or sub_texts_added or sub_texts_edited

    embedder = request.app.state.embedder
    if needs_reembed and not embedder.is_loaded:  # D5: only gate when re-embed needed
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="embedder not ready")
    # Validate template existence only when the field is actually being changed.
    effective_system = body.system_template if body.system_template is not None else current.system_template
    effective_instruction = body.instruction_template if body.instruction_template is not None else current.instruction_template
    if body.system_template is not None or body.instruction_template is not None:
        _validate_templates(effective_system, effective_instruction)

    # D16: schedule.mode is immutable — mode change requires delete + recreate
    if body.schedule is not None and body.schedule.mode != current.schedule.mode:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="schedule.mode is immutable; delete and recreate the intent to change mode",
        )

    # D22 step 3-5: write intents row + sub_texts inside a SINGLE outer transaction
    # (Loop 2 🔴-1). Splitting these into two transaction() blocks left intents
    # committed but sub_texts un-restorable if the child write raised — there was
    # no rollback path. Single transaction commits both or neither.
    try:
        async with transaction() as txn:
            await _update_intent_in_txn(txn, intent_id, body, current)
            if body.sub_texts is not None:
                await _sub_texts_replace_in_txn(txn, intent_id, body.sub_texts)
    except Exception as exc:
        # ROLLBACK was already issued by transaction()'s context manager exit
        # path (db/sqlite.py:97-100). Surface as 500 — caller retries.
        logger.error("PUT intent_id=%d SQLite write failed (rolled back): %s", intent_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to persist intent changes",
        ) from exc
    # D22 step 5: re-read once after the transaction commits so `updated.sub_texts`
    # reflects post-writeback state (also picks up any default-touched fields like updated_at).
    re_read = await get_intent(conn, intent_id)
    if re_read is None:  # pragma: no cover — concurrent delete; the UPDATE above would have errored first
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="intent vanished mid-update")
    updated = re_read

    # Slots gone after the update (D17 + reembed cleanup): when sub_texts shrank,
    # the alt_X for indices ≥ new count must be deleted from Qdrant explicitly —
    # upsert with `{main, alt_0..alt_{m-1}}` only writes those slots, it does
    # NOT remove higher-index slots that existed before.
    removed_slots = sorted(_slot_set(len(current.sub_texts)) - _slot_set(len(updated.sub_texts)))

    qdrant_client = request.app.state.qdrant.client
    new_slot_vecs: dict[str, list[float]] | None = None
    qdrant_written = False
    try:
        if needs_reembed:
            # D22 step 6: embed from `updated.*` so the post-writeback state is the source of truth
            texts_to_embed = [updated.text] + [st.text for st in updated.sub_texts]
            raw_vectors = await embedder.aembed(texts_to_embed)
            new_slot_vecs = _slot_dict_from_vectors(raw_vectors)
            # D22 step 7a: upsert entire vector dict (replaces named slots in-place)
            await upsert_intent_point(
                qdrant_client,
                intent_id,
                new_slot_vecs,
                payload=_build_payload(updated, embedder.model_version),
            )
            qdrant_written = True
            # Clean up any leftover slots from before — upsert doesn't delete them
            if removed_slots:
                await qdrant_client.delete_vectors(
                    collection_name=_INTENTS_ALIAS,
                    points=[intent_id],
                    vectors=removed_slots,
                )
        else:
            # No re-embed required. Two possible vector mutations: deleted slots OR none.
            # Label-only change and payload-only change both fall here and need payload sync.
            if removed_slots:
                # D17: shed alt_* slots without touching main / surviving alt_*
                await qdrant_client.delete_vectors(
                    collection_name=_INTENTS_ALIAS,
                    points=[intent_id],
                    vectors=removed_slots,
                )
                qdrant_written = True
            await update_intent_payload(
                qdrant_client,
                intent_id,
                payload=_build_payload(updated, embedder.model_version),
            )
    except Exception as exc:
        # SQLite rollback runs first so log messages below reflect true state (L2-I1).
        # D22 rollback: restore intents row AND sub_texts inside a SINGLE transaction
        # (Loop 2 🔴-1 — pairs with the single-transaction happy path above).
        sqlite_state = "rolled-back"
        try:
            async with transaction() as txn:
                await _update_intent_raw_in_txn(txn, intent_id, current)
                if body.sub_texts is not None:
                    await _sub_texts_replace_in_txn(txn, intent_id, current.sub_texts)
        except Exception as rb:
            sqlite_state = "rollback-failed"
            logger.error("PUT SQLite rollback failed for intent_id=%d: %s", intent_id, rb)

        # I2: if the new vector dict was already written server-side, attempt best-effort revert.
        # We do NOT delete_intent_point (would wipe main + alt_* including unrelated valid slots)
        # — instead leave the Qdrant point in its inconsistent state and log so operators can
        # reconcile. SQLite is the source of truth; subsequent PUT with matching body re-syncs.
        if qdrant_written:
            logger.error(
                "PUT inconsistency: intent_id=%d sqlite=%s qdrant=ahead-of-sqlite (no auto-revert)",
                intent_id,
                sqlite_state,
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="failed to sync intent vector",
        ) from exc

    # D22 step 8: clear match_seen when vector-set content (main or sub) changed and intent is enabled.
    if needs_reembed and updated.enabled:
        try:
            await clear_intent(conn, intent_id)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="failed to clear match deduplication state",
            ) from exc

    # D22 step 9: event-mode cache sync (R10 three-branch).
    # Placed after Qdrant confirm + clear_intent; outside the Qdrant try/except so no
    # cache rollback is needed (if we get here, all writes succeeded).
    if isinstance(updated.schedule, EventSchedule):
        cache = request.app.state.event_intent_cache
        if updated.enabled:
            cache_vectors: dict[str, list[float]] | None
            if needs_reembed and new_slot_vecs is not None:
                # R10 branch (a): we just embedded — use the fresh dict directly (defensive copy).
                cache_vectors = {k: list(v) for k, v in new_slot_vecs.items()}
            else:
                existing_entry = cache.get(intent_id)
                if existing_entry is not None:
                    # R10 branch (b): cache hit — reuse, minus any deleted slots.
                    cache_vectors = {
                        k: list(v)
                        for k, v in existing_entry.vectors.items()
                        if k not in removed_slots
                    }
                else:
                    # R10 branch (c): cache miss (intent was disabled or evicted) — retrieve from Qdrant.
                    pts = await qdrant_client.retrieve(
                        collection_name=_INTENTS_ALIAS, ids=[intent_id], with_vectors=True
                    )
                    cache_vectors = None
                    if pts:
                        raw = getattr(pts[0], "vector", None)
                        if isinstance(raw, dict):
                            cache_vectors = {}
                            for slot in ("main", "alt_0", "alt_1", "alt_2"):
                                v = extract_named_vector(pts[0], slot)
                                if v is not None and slot not in removed_slots:
                                    cache_vectors[slot] = v
            if cache_vectors is None or "main" not in cache_vectors:
                logger.error(
                    "PUT intent_id=%d: enabled=True but Qdrant main vector unresolvable; "
                    "event cache not updated. Disable or DELETE+POST to resolve.",
                    intent_id,
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="intent vector missing from Qdrant; delete and recreate the intent",
                )
            cache.add(
                intent_id,
                EventIntentEntry(
                    vectors=cache_vectors,
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
            if updated.enabled:
                register_intent_job(scheduler, updated, request.app, fire_immediately=True)
            else:
                unregister_intent_job(scheduler, intent_id)
        elif updated.enabled:
            if text_changed or schedule_changed:
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
        # match_seen rows cascade automatically via ON DELETE CASCADE (D10);
        # intent_sub_texts also cascades via FK ON DELETE CASCADE.
        await delete_intent(conn, intent_id)
    except Exception as exc:
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

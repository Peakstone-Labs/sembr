# SPDX-License-Identifier: Apache-2.0
"""Intents collection bootstrap and CRUD.

Idempotent: checks existence and layout before mutating.
No quantization: intent vectors are query-side; precision matters more than memory
savings at < 1000 entries (~4 MB raw at 1024-dim).

Layout: named-vector dict {main, alt_0, alt_1, alt_2} per point. The "_mv"
suffix on the collection name marks the layout version orthogonally to
embedder.model_version, so a future BGE-M3 → other embedding migration
flips the model-version segment without churning the layout marker.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite
    from qdrant_client import AsyncQdrantClient

    from sembr.embedder.base import BaseEmbedder

logger = logging.getLogger(__name__)

ALIAS_NAME = "intents_current"

# v1 caps sub_texts at 3; all four slots are declared in the collection so a
# point may omit alt_* slots and query_points(using="alt_2") on a slot-less point
# simply yields no hits.
_SLOT_NAMES: tuple[str, ...] = ("main", "alt_0", "alt_1", "alt_2")

# BGE-M3 / SiliconFlow batch ceiling (sembr CLAUDE.md tech-stack table).
_EMBED_BATCH_MAX = 32


def collection_name(model_version: str) -> str:
    """Legacy unnamed-vector collection name. Retained so migration can detect
    the old layout when probing an existing alias target.
    """
    return f"intents_{model_version}"


def multi_vec_collection_name(model_version: str) -> str:
    """Named-vector layout collection name."""
    return f"intents_{model_version}_mv"


async def _layout_is_named_vec(client: "AsyncQdrantClient", collection: str) -> bool:
    """True iff `collection` has a named-vector dict layout that includes the `main` slot.

    A False answer means either the collection does not exist, or it uses the
    legacy unnamed-vector layout, or its dict-layout is missing the main slot
    (corrupt state).
    """
    try:
        info = await client.get_collection(collection)
    except Exception:
        return False
    vectors_cfg = getattr(info.config.params, "vectors", None)
    if not isinstance(vectors_cfg, dict):
        return False
    return "main" in vectors_cfg


async def _select_intent_main_texts(conn: "aiosqlite.Connection") -> list[tuple[int, str]]:
    """Pulls (id, text) pairs for the migration re-embed. ORDER BY id for log determinism."""
    async with conn.execute("SELECT id, text FROM intents ORDER BY id ASC") as cur:
        rows = await cur.fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


async def _build_mv_collection(
    client: "AsyncQdrantClient",
    name: str,
    dim: int,
) -> None:
    from qdrant_client.models import Distance, VectorParams  # noqa: PLC0415

    await client.create_collection(
        collection_name=name,
        vectors_config={
            slot: VectorParams(size=dim, distance=Distance.COSINE, on_disk=False)
            for slot in _SLOT_NAMES
        },
    )
    logger.info("created Qdrant collection %r (named-vector layout)", name)


async def _migrate_reembed_and_upsert(
    client: "AsyncQdrantClient",
    embedder: "BaseEmbedder",
    mv_name: str,
    rows: list[tuple[int, str]],
) -> None:
    """Embed all main texts and upsert into the new _mv collection.

    The embedder is loaded lazily on first migration run — lifespan launches
    `embedder.load()` as a background task AFTER this function, so on legacy
    DBs we'd otherwise hit "embedder not loaded". load() is idempotent and
    never raises; we re-check is_loaded after and fail loudly if the probe
    didn't succeed (bad API key / network) so lifespan aborts with a clear
    message instead of crashing on the first embed call.
    """
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    if not rows:
        return
    if not embedder.is_loaded:
        logger.info(
            "ensure_intents_collection: %d intents need re-embed but embedder not yet loaded; "
            "running embedder.load() synchronously before migration",
            len(rows),
        )
        await embedder.load()
        if not embedder.is_loaded:
            raise RuntimeError(
                "embedder.load() probe failed during intents migration "
                "(bad EMBEDDER_API_KEY or upstream outage); cannot re-embed "
                "intent main texts. Fix the embedder configuration and restart."
            )
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    vectors: list[list[float]] = []
    for i in range(0, len(texts), _EMBED_BATCH_MAX):
        batch = texts[i : i + _EMBED_BATCH_MAX]
        vectors.extend(await embedder.aembed(batch))
    model_version = embedder.model_version
    points = [
        PointStruct(
            id=ids[i],
            vector={"main": vectors[i]},
            payload={"intent_id": ids[i], "embedding_model_version": model_version},
        )
        for i in range(len(ids))
    ]
    await client.upsert(collection_name=mv_name, points=points, wait=True)
    logger.info("migration: re-embedded + upserted %d intents into %r", len(rows), mv_name)


async def _flip_alias(
    client: "AsyncQdrantClient",
    alias_map: dict[str, str],
    target: str,
) -> None:
    """Move ALIAS_NAME to `target`. Uses delete+create in one ops batch when alias exists."""
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        DeleteAlias,
        DeleteAliasOperation,
    )

    ops: list[Any] = []
    if ALIAS_NAME in alias_map:
        ops.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=ALIAS_NAME)))
    ops.append(
        CreateAliasOperation(
            create_alias=CreateAlias(collection_name=target, alias_name=ALIAS_NAME)
        )
    )
    await client.update_collection_aliases(change_aliases_operations=ops)
    logger.info("flipped alias %r → %r", ALIAS_NAME, target)


async def ensure_intents_collection(
    client: "AsyncQdrantClient",
    embedder: "BaseEmbedder",
    conn: "aiosqlite.Connection | None" = None,
) -> None:
    """Bootstrap or migrate the intents collection to the named-vector layout.

    Cases:
      1. Fresh DB / no _mv: create _mv with named-vec layout, no re-embed, flip alias.
      2. Alias points to legacy unnamed-vec collection: create _mv, re-embed all
         main texts, flip alias. Old collection retained for manual rollback.
      3. Alias already points to a named-vec _mv with the `main` slot present: no-op.
      4. _mv exists but Qdrant count != SQLite count (partial-failure recovery):
         delete + recreate _mv, redo re-embed + alias flip.
    """
    mv_name = multi_vec_collection_name(embedder.model_version)

    all_collections = await client.get_collections()
    existing_names = {c.name for c in all_collections.collections}

    aliases_resp = await client.get_aliases()
    alias_map = {a.alias_name: a.collection_name for a in aliases_resp.aliases}

    # Case 3: no-op
    current_target = alias_map.get(ALIAS_NAME)
    if current_target and await _layout_is_named_vec(client, current_target):
        logger.debug("intents collection already at named-vector layout (%r)", current_target)
        return

    # SQLite row count: used by partial-recovery probe and to drive the re-embed loop
    rows = await _select_intent_main_texts(conn) if conn is not None else []
    expected_count = len(rows)

    # Case 4: partial-recovery check requires both exact count AND ID-set equality
    # — "count plausible but ID set diverged" is a real failure mode when a partial
    # migration left the _mv collection stale relative to SQLite (e.g. user added
    # an intent between two failed migration attempts).
    if mv_name in existing_names:
        mismatch_reason: str | None = None
        if not await _layout_is_named_vec(client, mv_name):
            mismatch_reason = "layout"
        else:
            try:
                actual = (await client.count(collection_name=mv_name, exact=True)).count
            except Exception as exc:
                logger.warning(
                    "recovery probe: client.count failed (%s); treating as mismatch", exc
                )
                actual = -1
                mismatch_reason = f"count probe failed ({exc})"
            if mismatch_reason is None and actual != expected_count:
                mismatch_reason = f"count={actual} expected={expected_count}"
            if mismatch_reason is None and expected_count > 0:
                # ID-level cross-check via scroll. N<1000 in practice so one or
                # two scroll pages cover the whole collection; the round-trip
                # cost is paid once at startup. Catches the "count equal but IDs
                # diverged" silent inconsistency window.
                scroll_ids: set[int] = set()
                offset = None
                try:
                    while True:
                        pts, offset = await client.scroll(
                            collection_name=mv_name,
                            limit=256,
                            offset=offset,
                            with_payload=False,
                            with_vectors=False,
                        )
                        scroll_ids.update(int(p.id) for p in pts)
                        if offset is None:
                            break
                except Exception as exc:
                    logger.warning(
                        "recovery ID probe: scroll failed (%s); treating as mismatch", exc
                    )
                    mismatch_reason = f"scroll probe failed ({exc})"
                else:
                    expected_ids = {r[0] for r in rows}
                    if scroll_ids != expected_ids:
                        diff_short = (
                            f"missing={sorted(expected_ids - scroll_ids)[:5]} "
                            f"extra={sorted(scroll_ids - expected_ids)[:5]}"
                        )
                        mismatch_reason = f"ID set diverged ({diff_short})"
        if mismatch_reason is not None:
            logger.warning("recovery: _mv=%r %s — recreating", mv_name, mismatch_reason)
            await client.delete_collection(mv_name)
            existing_names.discard(mv_name)

    if mv_name not in existing_names:
        await _build_mv_collection(client, mv_name, embedder.dim)
        await _migrate_reembed_and_upsert(client, embedder, mv_name, rows)

    # Flip alias only after the new collection holds the expected row count.
    await _flip_alias(client, alias_map, mv_name)


async def upsert_intent_point(
    client: "AsyncQdrantClient",
    intent_id: int,
    vectors: list[float] | dict[str, list[float]],
    payload: dict[str, Any],
) -> None:
    """Insert or replace the intent point. point_id == SQLite intent_id.

    Accepts either `list[float]` (legacy single-vector form; auto-wrapped as
    {"main": vec}) or `dict[str, list[float]]` (named-vector form for the
    multi-vector layout). Phase 3 will migrate callers to always pass dict and
    this shim can be dropped.
    """
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    if isinstance(vectors, list):
        vectors_payload: dict[str, list[float]] | list[float] = {"main": vectors}
    else:
        vectors_payload = vectors

    await client.upsert(
        collection_name=ALIAS_NAME,
        points=[PointStruct(id=intent_id, vector=vectors_payload, payload=payload)],
    )


async def update_intent_payload(
    client: "AsyncQdrantClient",
    intent_id: int,
    payload: dict[str, Any],
) -> None:
    """Replace stored payload atomically without touching vectors.

    Uses overwrite_payload (replace) not set_payload (merge) so removed keys
    do not silently persist.
    """
    await client.overwrite_payload(
        collection_name=ALIAS_NAME,
        payload=payload,
        points=[intent_id],
    )


async def delete_intent_point(client: "AsyncQdrantClient", intent_id: int) -> None:
    """Remove the entire intent point (all named vectors). Caller deletes SQLite row."""
    from qdrant_client.models import PointIdsList  # noqa: PLC0415

    await client.delete(
        collection_name=ALIAS_NAME,
        points_selector=PointIdsList(points=[intent_id]),
    )

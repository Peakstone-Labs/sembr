"""Intents collection bootstrap and CRUD.

Idempotent: checks existence before creating collection and alias.
No quantization: intent vectors are query-side in the matcher's `query_points`
calls; precision matters more than memory savings at < 1000 entries
(~4 MB raw at 1024-dim).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

    from sembr.embedder.base import BaseEmbedder

logger = logging.getLogger(__name__)

ALIAS_NAME = "intents_current"


def collection_name(model_version: str) -> str:
    """Versioned collection name for the intents store.

    The alias `intents_current` points at the active version; readers should always
    address points via the alias, never via the versioned name directly.
    """
    return f"intents_{model_version}"


async def ensure_intents_collection(
    client: "AsyncQdrantClient", embedder: "BaseEmbedder"
) -> None:
    """Create the intents collection and alias if missing. Idempotent.

    Collection name and vector dim are derived from the embedder so a backend swap
    (e.g. bge-m3 → another 1024-dim model, or a different-dim model entirely) flips
    the storage in lockstep without any duplicated literals to keep in sync.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        Distance,
        VectorParams,
    )

    name = collection_name(embedder.model_version)

    collections = await client.get_collections()
    existing_names = {c.name for c in collections.collections}

    if name not in existing_names:
        await client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(
                size=embedder.dim,
                distance=Distance.COSINE,
                on_disk=False,  # full memory; query-side vectors need precision over savings
            ),
        )
        logger.info("created Qdrant collection %r", name)

    all_aliases = await client.get_aliases()
    alias_map = {a.alias_name: a.collection_name for a in all_aliases.aliases}

    if ALIAS_NAME not in alias_map:
        await client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=name,
                        alias_name=ALIAS_NAME,
                    )
                )
            ],
        )
        logger.info("created alias %r → %r", ALIAS_NAME, name)
    elif alias_map[ALIAS_NAME] != name:
        logger.warning(
            "alias %r already points to %r, not %r — leaving as-is "
            "(alias migration is owned by the model-upgrade flow, not bootstrap)",
            ALIAS_NAME,
            alias_map[ALIAS_NAME],
            name,
        )


async def upsert_intent_point(
    client: "AsyncQdrantClient",
    intent_id: int,
    vector: list[float],
    payload: dict[str, Any],
) -> None:
    """Insert or replace the intent vector. point_id == SQLite intent_id."""
    from qdrant_client.models import PointStruct  # noqa: PLC0415

    await client.upsert(
        collection_name=ALIAS_NAME,
        points=[PointStruct(id=intent_id, vector=vector, payload=payload)],
    )


async def update_intent_payload(
    client: "AsyncQdrantClient",
    intent_id: int,
    payload: dict[str, Any],
) -> None:
    """Replace the stored payload atomically without re-uploading the vector.

    Uses overwrite_payload (replace semantics) not set_payload (merge semantics) so
    that keys removed from the payload-builder in future do not silently persist in
    Qdrant storage. The matcher reads enabled/threshold from this payload, so stale
    keys would be a correctness hazard.
    """
    await client.overwrite_payload(
        collection_name=ALIAS_NAME,
        payload=payload,
        points=[intent_id],
    )


async def delete_intent_point(client: "AsyncQdrantClient", intent_id: int) -> None:
    """Remove the intent vector. Caller is responsible for deleting the SQLite row."""
    from qdrant_client.models import PointIdsList  # noqa: PLC0415

    await client.delete(
        collection_name=ALIAS_NAME,
        points_selector=PointIdsList(points=[intent_id]),
    )

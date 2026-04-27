"""intents_bge-m3_v1 collection bootstrap and CRUD.

Idempotent: checks existence before creating collection and alias.
No quantization (O2-B): intent vectors are query-side in search_batch;
precision > memory savings at < 1000 entries / 4 MB raw at MVP scale.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

COLLECTION_NAME = "intents_bge-m3_v1"
ALIAS_NAME = "intents_current"


async def ensure_intents_collection(client: "AsyncQdrantClient") -> None:
    """Create the intents collection and alias if missing. Idempotent."""
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        Distance,
        VectorParams,
    )

    collections = await client.get_collections()
    existing_names = {c.name for c in collections.collections}

    if COLLECTION_NAME not in existing_names:
        await client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(
                size=1024,
                distance=Distance.COSINE,
                on_disk=False,  # O2-B: full memory; query-side vectors need precision > savings
            ),
        )
        logger.info("created Qdrant collection %r", COLLECTION_NAME)

    all_aliases = await client.get_aliases()
    alias_map = {a.alias_name: a.collection_name for a in all_aliases.aliases}

    if ALIAS_NAME not in alias_map:
        await client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=COLLECTION_NAME,
                        alias_name=ALIAS_NAME,
                    )
                )
            ],
        )
        logger.info("created alias %r → %r", ALIAS_NAME, COLLECTION_NAME)
    elif alias_map[ALIAS_NAME] != COLLECTION_NAME:
        logger.warning(
            "alias %r already points to %r, not %r — skipping",
            ALIAS_NAME,
            alias_map[ALIAS_NAME],
            COLLECTION_NAME,
        )


async def upsert_intent_point(
    client: "AsyncQdrantClient",
    intent_id: int,
    vector: list[float],
    payload: dict[str, Any],
) -> None:
    """Insert or replace the intent vector. point_id = SQLite intent_id (D2 / O3-A)."""
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
    """Replace the stored payload atomically without re-uploading the vector (D7 / I1).

    Uses overwrite_payload (replace semantics) not set_payload (merge semantics) so that
    keys removed from _build_payload in future don't silently persist in Qdrant storage.
    Matcher reads enabled/threshold from this payload — stale keys are a correctness hazard.
    """
    await client.overwrite_payload(
        collection_name=ALIAS_NAME,
        payload=payload,
        points=[intent_id],
    )


async def delete_intent_point(client: "AsyncQdrantClient", intent_id: int) -> None:
    """Remove the intent vector. D8: caller deletes the SQLite row after this returns."""
    from qdrant_client.models import PointIdsList  # noqa: PLC0415

    await client.delete(
        collection_name=ALIAS_NAME,
        points_selector=PointIdsList(points=[intent_id]),
    )

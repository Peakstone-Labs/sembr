"""News collection bootstrap + write helpers.

Idempotent: checks existence before creating collection and alias.
Alias switching for model upgrades is out of scope here — owned by a future
model-upgrade flow, not bootstrap.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

    from sembr.embedder.base import BaseEmbedder

logger = logging.getLogger(__name__)

ALIAS_NAME = "news_current"


def md5_to_uuid(md5: str) -> str:
    """Deterministic md5 → Qdrant point UUID mapping.

    `feed_items.md5` is 32-char lowercase hex; `uuid.UUID(hex=md5)` produces a
    well-formed UUID string we use as the news Qdrant point ID. Idempotent:
    `uuid_to_md5(md5_to_uuid(m)) == m`.
    """
    return str(uuid.UUID(hex=md5))


def uuid_to_md5(uuid_str: str) -> str:
    """Inverse of `md5_to_uuid`; used by reconcile / TTL paths to map Qdrant
    point IDs back to the SQLite ``feed_items.md5`` column for cascade-delete.
    """
    return uuid.UUID(uuid_str).hex


def collection_name(model_version: str) -> str:
    """Versioned collection name for the news store.

    Production callers should write through `ALIAS_NAME` (`news_current`); the
    versioned name only matters for bootstrap and alias migration.
    """
    return f"news_{model_version}"


async def ensure_news_collection(
    client: "AsyncQdrantClient", embedder: "BaseEmbedder"
) -> None:
    """Create the news collection and alias if either is missing. Idempotent.

    Collection name and vector dim are derived from the embedder so a backend swap
    flips the storage in lockstep. If `news_current` already points to a different
    collection, logs a warning and leaves it unchanged — alias migration belongs to
    the model-upgrade flow, not bootstrap.

    `qdrant_client` models are imported lazily so this module remains importable
    on a development machine without `qdrant_client` installed.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        Distance,
        PayloadSchemaType,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
        TextIndexParams,
        TokenizerType,
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
                on_disk=True,
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,
                ),
            ),
        )
        logger.info("created Qdrant collection %r", name)

    # Payload index on ingested_at_ts: required for the dashboard's
    # scroll(order_by="ingested_at_ts") "latest articles" listing. Qdrant rejects
    # order_by on un-indexed fields; create_payload_index is idempotent so
    # repeated startup is safe.
    await client.create_payload_index(
        collection_name=name,
        field_name="ingested_at_ts",
        field_schema=PayloadSchemaType.INTEGER,
    )

    # Payload index on feed_id: the Feeds tab drill-down filters news_current by
    # feed_id. Without this index Qdrant degrades to a full collection scan on
    # every expand. Idempotent.
    await client.create_payload_index(
        collection_name=name,
        field_name="feed_id",
        field_schema=PayloadSchemaType.INTEGER,
    )

    # Text payload index on title: required for the dashboard's qdrant-articles
    # filter `title_q` (D7/D8). Qdrant's MatchText condition silently scans the
    # whole collection without a text index — D7's query plan needs this in
    # place before the first filtered scroll. Idempotent: create_payload_index
    # is a no-op when the index already exists.
    #
    # WORD tokenizer + lowercase = case-insensitive whole-token match.
    # CJK input gets char-level tokenization under WORD; this is an accepted
    # v1 trade-off (design D / R9). MULTILINGUAL is the post-1.0 candidate.
    await client.create_payload_index(
        collection_name=name,
        field_name="title",
        field_schema=TextIndexParams(
            type="text",
            tokenizer=TokenizerType.WORD,
            lowercase=True,
            min_token_len=1,
            max_token_len=20,
        ),
    )

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


async def upsert_news_points(
    client: "AsyncQdrantClient",
    points: list[Any],
    *,
    wait: bool = True,
) -> None:
    """Upsert article points through the `news_current` alias.

    Caller owns `PointStruct` construction (the embedder worker has model-version
    metadata it needs to inject into payloads); this helper exists so that the
    collection alias is not duplicated at every write site.
    """
    await client.upsert(
        collection_name=ALIAS_NAME,
        points=points,
        wait=wait,
    )

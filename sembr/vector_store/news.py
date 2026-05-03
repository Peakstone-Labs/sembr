"""news_bge-m3_v1 collection bootstrap.

Idempotent: checks existence before creating collection and alias.
Alias switching for model upgrades is out of scope for MVP (D9).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)

COLLECTION_NAME = "news_bge-m3_v1"
ALIAS_NAME = "news_current"


async def ensure_news_collection(client: "AsyncQdrantClient") -> None:
    """Create the news collection and alias if either is missing. Idempotent (D8, D9).

    If news_current already points to a different collection, logs a warning and
    leaves it unchanged — alias migration belongs to the 0.2.0 alias-switch feature.

    qdrant_client models are imported lazily so this module is importable on the
    Windows dev machine without qdrant_client installed.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        Distance,
        PayloadSchemaType,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
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
                on_disk=True,
            ),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=True,
                ),
            ),
        )
        logger.info("created Qdrant collection %r", COLLECTION_NAME)

    # Payload index on ingested_at_ts: required for the dashboard's
    # scroll(order_by="ingested_at_ts") "latest articles" listing. Qdrant rejects
    # order_by on un-indexed fields; create_payload_index is idempotent so
    # repeated startup is safe.
    await client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="ingested_at_ts",
        field_schema=PayloadSchemaType.INTEGER,
    )

    # Payload index on feed_id (D2 / R2): the Feeds tab drill-down filters
    # news_current by feed_id. Without this index Qdrant degrades to a full
    # collection scan on every expand. Idempotent.
    await client.create_payload_index(
        collection_name=COLLECTION_NAME,
        field_name="feed_id",
        field_schema=PayloadSchemaType.INTEGER,
    )

    # Check alias globally to detect if news_current points elsewhere (D9)
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
            "alias %r already points to %r, not %r — skipping (D9)",
            ALIAS_NAME,
            alias_map[ALIAS_NAME],
            COLLECTION_NAME,
        )

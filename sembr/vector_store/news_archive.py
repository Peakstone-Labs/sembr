# SPDX-License-Identifier: Apache-2.0
"""News archive collection bootstrap + payload enrichment + write helpers.

The archive permanently keeps every news point the TTL job expires out of
``news_current``, vector included, so old articles stay semantically
searchable. Storage is disk-first — quantization cache NOT pinned in RAM,
HNSW graph on disk, payload indexes on disk — because the shared Qdrant
container runs under a hard memory cap and the hot path (matcher / ingest)
must never be squeezed by unbounded archive growth. Archive queries are rare
ad-hoc lookups where disk-latency is acceptable.

Bootstrap is idempotent and mirrors ``vector_store/news.py``. Alias switching
for model upgrades is out of scope here — owned by a future model-upgrade
flow; archive payloads keep ``embedding_model_version`` per point so that flow
can decide re-embed / stratify / freeze later.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from qdrant_client import AsyncQdrantClient

    from sembr.embedder.base import BaseEmbedder

try:
    from qdrant_client.models import PointStruct
except ImportError:
    # qdrant_client not installed on lightweight dev machines; tests stub the
    # models module. A plain dataclass keeps the payload API intact (mirrors
    # embedder/scheduler.py).
    from dataclasses import dataclass as _dc

    @_dc
    class PointStruct:  # type: ignore[no-redef]
        id: str
        vector: list
        payload: dict


from sembr.vector_store.qdrant import extract_point_vector

logger = logging.getLogger(__name__)

ARCHIVE_ALIAS = "news_archive"


def archive_collection_name(model_version: str) -> str:
    """Versioned physical collection name for the archive store.

    Production callers must write/query through ``ARCHIVE_ALIAS``; the
    versioned name only matters for bootstrap and future alias migration.
    """
    return f"news_archive_{model_version}"


async def ensure_news_archive_collection(client: AsyncQdrantClient, embedder: BaseEmbedder) -> None:
    """Create the archive collection, its payload indexes and alias. Idempotent.

    Runs unconditionally at startup (even when archiving is disabled) so the
    search endpoints never race a missing collection; creating an empty
    collection is free.

    Storage deviates from the news collection on purpose:

    - quantization ``always_ram=False``: the INT8 cache grows ~1 KB/point
      forever; pinning it in RAM would walk the shared container into its
      memory cap within a couple of years and OOM-kill would take the hot
      path down with it.
    - ``hnsw_config.on_disk=True``: the graph is another ~128 B/point of
      would-be resident memory; disk traversal is fine for ad-hoc queries.
    - every payload index ``on_disk=True`` for the same reason.

    ``qdrant_client`` models are imported lazily so this module remains
    importable without ``qdrant_client`` installed.
    """
    from qdrant_client.models import (  # noqa: PLC0415
        CreateAlias,
        CreateAliasOperation,
        Distance,
        HnswConfigDiff,
        IntegerIndexParams,
        KeywordIndexParams,
        ScalarQuantization,
        ScalarQuantizationConfig,
        ScalarType,
        TextIndexParams,
        TokenizerType,
        VectorParams,
    )

    name = archive_collection_name(embedder.model_version)

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
            hnsw_config=HnswConfigDiff(on_disk=True),
            quantization_config=ScalarQuantization(
                scalar=ScalarQuantizationConfig(
                    type=ScalarType.INT8,
                    always_ram=False,
                ),
            ),
        )
        logger.info("created Qdrant archive collection %r", name)

    # Range-only integer indexes: time windows + ordered listing + length
    # floor. `lookup=False` drops the exact-match hashmap the archive never
    # uses for these fields. ingested_at_ts also serves scroll(order_by=...),
    # which Qdrant rejects on un-indexed fields.
    for range_field in ("ingested_at_ts", "published_at_ts", "body_len"):
        await client.create_payload_index(
            collection_name=name,
            field_name=range_field,
            field_schema=IntegerIndexParams(type="integer", lookup=False, range=True, on_disk=True),
        )

    # Lookup-only integer indexes: multi-select / exclusion matching.
    # matched_intents is a list payload — Qdrant indexes array fields
    # per-element, so MatchAny works directly.
    for lookup_field in ("feed_id", "matched_intents"):
        await client.create_payload_index(
            collection_name=name,
            field_name=lookup_field,
            field_schema=IntegerIndexParams(type="integer", lookup=True, range=False, on_disk=True),
        )

    # Keyword indexes: exact-match filters on derived fields.
    for keyword_field in ("url_domain", "lang"):
        await client.create_payload_index(
            collection_name=name,
            field_name=keyword_field,
            field_schema=KeywordIndexParams(type="keyword", on_disk=True),
        )

    # Title keyword search. MULTILINGUAL tokenizer is required for CJK: WORD
    # only splits on whitespace/punctuation, so a Chinese title becomes one
    # long token that max_token_len drops and MatchText returns 0 hits
    # (same hard-won parameters as the news_current title index).
    await client.create_payload_index(
        collection_name=name,
        field_name="title",
        field_schema=TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            lowercase=True,
            min_token_len=1,
            max_token_len=20,
            on_disk=True,
        ),
    )

    all_aliases = await client.get_aliases()
    alias_map = {a.alias_name: a.collection_name for a in all_aliases.aliases}

    if ARCHIVE_ALIAS not in alias_map:
        await client.update_collection_aliases(
            change_aliases_operations=[
                CreateAliasOperation(
                    create_alias=CreateAlias(
                        collection_name=name,
                        alias_name=ARCHIVE_ALIAS,
                    )
                )
            ],
        )
        logger.info("created alias %r → %r", ARCHIVE_ALIAS, name)
    elif alias_map[ARCHIVE_ALIAS] != name:
        # ERROR (not warning, unlike the news bootstrap): the archive is
        # queried so rarely that a boot-time warning would be missed for
        # months while semantic search silently degrades against a
        # different-generation vector space.
        logger.error(
            "alias %r already points to %r, not %r — leaving as-is "
            "(alias migration is owned by the model-upgrade flow, not "
            "bootstrap); archive semantic search runs against a "
            "different-generation collection until that flow resolves it",
            ARCHIVE_ALIAS,
            alias_map[ARCHIVE_ALIAS],
            name,
        )


# ---------------------------------------------------------------------------
# Payload enrichment — pure functions defining the archive payload schema.
# ---------------------------------------------------------------------------


def parse_published_at_ts(published_at: Any) -> int | None:
    """Parse the payload ``published_at`` ISO string to epoch seconds.

    Both collector paths (rss feedparser timestamps, newsapi dateTime) emit
    tz-aware UTC datetimes, so a naive string can only come from an unknown
    writer — treated as unparseable (field stays absent) instead of guessing
    a timezone and silently shifting the article by hours. Absent fields
    never match a Range filter, which is the intended "unknown time" query
    semantics.
    """
    if not published_at or not isinstance(published_at, str):
        return None
    try:
        dt = datetime.fromisoformat(published_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return int(dt.timestamp())


def detect_lang(title: str, body: str) -> str:
    """Cheap zh/en/other tag from CJK-vs-latin letter counts.

    Samples the title plus the first 2000 body chars — enough to classify
    while keeping migration cost flat for very long articles.
    """
    sample = f"{title} {body[:2000]}"
    cjk = sum(1 for ch in sample if "一" <= ch <= "鿿")
    latin = sum(1 for ch in sample if ("a" <= ch <= "z") or ("A" <= ch <= "Z"))
    informative = cjk + latin
    if informative and cjk / informative >= 0.30:
        return "zh"
    if latin >= 20:
        return "en"
    return "other"


def extract_url_domain(url: Any) -> str | None:
    """Lowercased hostname without a leading ``www.``, or None."""
    if not url or not isinstance(url, str):
        return None
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().removeprefix("www.")
    return host or None


def build_archive_point(
    point: Any, matched_intents: list[int], archived_at_ts: int
) -> PointStruct | None:
    """Enriched archive point from a retrieved ``news_current`` point.

    Returns None when the source point carries no usable vector — the caller
    counts it as poisoned and leaves it in ``news_current``, so a single bad
    point can never stall the migration run or get silently dropped.

    ``matched_intents`` must be read from ``match_seen`` BEFORE the cascade
    delete of the same run — those rows are gone afterwards. Always written
    (possibly empty) so the field is filterable across every archived point.
    """
    vector = extract_point_vector(point)
    if vector is None:
        return None
    payload = dict(getattr(point, "payload", None) or {})
    title = payload.get("title")
    body = payload.get("body")
    title = title if isinstance(title, str) else ""
    body = body if isinstance(body, str) else ""

    published_at_ts = parse_published_at_ts(payload.get("published_at"))
    if published_at_ts is not None:
        payload["published_at_ts"] = published_at_ts
    payload["body_len"] = len(body)
    payload["lang"] = detect_lang(title, body)
    url_domain = extract_url_domain(payload.get("url"))
    if url_domain is not None:
        payload["url_domain"] = url_domain
    payload["matched_intents"] = matched_intents
    payload["archived_at_ts"] = archived_at_ts
    return PointStruct(id=str(point.id), vector=vector, payload=payload)


async def upsert_archive_points(
    client: AsyncQdrantClient,
    points: list[Any],
    *,
    wait: bool = True,
) -> None:
    """Upsert enriched points through the archive alias.

    ``wait=True`` is required by the migration invariant: the TTL job may
    only delete from ``news_current`` after Qdrant confirms the archive
    write — an unacknowledged upsert followed by a delete is a data-loss
    window.
    """
    await client.upsert(
        collection_name=ARCHIVE_ALIAS,
        points=points,
        wait=wait,
    )

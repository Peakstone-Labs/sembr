# SPDX-License-Identifier: Apache-2.0
"""Bootstrap tests for ensure_news_archive_collection.

Locks the disk-first storage profile (quantization NOT pinned in RAM, HNSW
graph on disk, every payload index on disk) — the archive shares one
memory-capped Qdrant container with the hot path, so any of these flipping
back to the news_current defaults would silently walk the container into its
cap as the archive grows.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.vector_store.news_archive import ensure_news_archive_collection

_EXPECTED_INDEX_FIELDS = {
    "ingested_at_ts",
    "published_at_ts",
    "body_len",
    "feed_id",
    "matched_intents",
    "url_domain",
    "lang",
    "title",
}


def _fake_client(*, existing: list[str] | None = None, aliases: dict[str, str] | None = None):
    client = MagicMock()
    client.get_collections = AsyncMock(
        return_value=SimpleNamespace(
            collections=[SimpleNamespace(name=n) for n in (existing or [])]
        )
    )
    client.create_collection = AsyncMock()
    client.create_payload_index = AsyncMock()
    client.get_aliases = AsyncMock(
        return_value=SimpleNamespace(
            aliases=[
                SimpleNamespace(alias_name=a, collection_name=c) for a, c in (aliases or {}).items()
            ]
        )
    )
    client.update_collection_aliases = AsyncMock()
    return client


_EMBEDDER = SimpleNamespace(model_version="bge-m3_v1", dim=1024)


@pytest.mark.asyncio
async def test_ensure_archive_creates_disk_first_collection():
    client = _fake_client()

    await ensure_news_archive_collection(client, _EMBEDDER)

    client.create_collection.assert_awaited_once()
    kwargs = client.create_collection.call_args.kwargs
    assert kwargs["collection_name"] == "news_archive_bge-m3_v1"

    vectors = kwargs["vectors_config"]
    assert vectors.size == 1024
    assert vectors.on_disk is True

    # Quantization cache must NOT be RAM-pinned (unlike news_current).
    quant = kwargs["quantization_config"]
    assert quant.scalar.always_ram is False

    # HNSW graph on disk.
    assert kwargs["hnsw_config"].on_disk is True


@pytest.mark.asyncio
async def test_ensure_archive_creates_all_payload_indexes_on_disk():
    client = _fake_client()

    await ensure_news_archive_collection(client, _EMBEDDER)

    calls = client.create_payload_index.call_args_list
    fields = {c.kwargs["field_name"] for c in calls}
    assert fields == _EXPECTED_INDEX_FIELDS
    assert len(calls) == len(_EXPECTED_INDEX_FIELDS)

    by_field = {c.kwargs["field_name"]: c.kwargs["field_schema"] for c in calls}

    # Every index stays on disk.
    for field, schema in by_field.items():
        assert getattr(schema, "on_disk", None) is True, field

    # Range-vs-lookup split on the integer indexes.
    for field in ("ingested_at_ts", "published_at_ts", "body_len"):
        assert by_field[field].range is True
        assert by_field[field].lookup is False
    for field in ("feed_id", "matched_intents"):
        assert by_field[field].lookup is True
        assert by_field[field].range is False

    # Title text index keeps the CJK-safe tokenizer parameters.
    title = by_field["title"]
    tokenizer = title.tokenizer
    tokenizer_str = tokenizer.value if hasattr(tokenizer, "value") else str(tokenizer)
    assert "multilingual" in tokenizer_str.lower()
    assert title.lowercase is True
    assert title.min_token_len == 1
    assert title.max_token_len == 20


@pytest.mark.asyncio
async def test_ensure_archive_creates_alias():
    client = _fake_client()

    await ensure_news_archive_collection(client, _EMBEDDER)

    client.update_collection_aliases.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_archive_idempotent_when_exists():
    client = _fake_client(
        existing=["news_archive_bge-m3_v1"],
        aliases={"news_archive": "news_archive_bge-m3_v1"},
    )

    await ensure_news_archive_collection(client, _EMBEDDER)

    client.create_collection.assert_not_called()
    client.update_collection_aliases.assert_not_called()
    # Index creation is server-side idempotent and always re-attempted.
    assert client.create_payload_index.call_count == len(_EXPECTED_INDEX_FIELDS)


@pytest.mark.asyncio
async def test_ensure_archive_alias_mismatch_errors_and_leaves_alias(caplog):
    client = _fake_client(
        existing=["news_archive_bge-m3_v1"],
        aliases={"news_archive": "news_archive_old-model"},
    )

    with caplog.at_level("ERROR", logger="sembr.vector_store.news_archive"):
        await ensure_news_archive_collection(client, _EMBEDDER)

    client.update_collection_aliases.assert_not_called()
    assert any("news_archive" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)

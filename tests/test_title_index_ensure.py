"""Verify ensure_news_collection creates the title text-index (design D8).

Tests that the lazy-import block exposes ``TextIndexParams`` and ``TokenizerType``
(memory: dev would silently NameError at runtime if missed) and that the
field_schema sent to qdrant is type=text + tokenizer=MULTILINGUAL + lowercase=True.
The MULTILINGUAL tokenizer is required for CJK title search — WORD treats a
Chinese title as a single token that ``max_token_len`` then drops.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sembr.vector_store.news import ensure_news_collection


@pytest.mark.asyncio
async def test_ensure_news_collection_creates_title_text_index():
    fake_client = MagicMock()

    # collection doesn't exist yet → create_collection is invoked once
    fake_client.get_collections = AsyncMock(return_value=SimpleNamespace(collections=[]))
    fake_client.create_collection = AsyncMock()
    fake_client.create_payload_index = AsyncMock()
    fake_client.get_aliases = AsyncMock(return_value=SimpleNamespace(aliases=[]))
    fake_client.update_collection_aliases = AsyncMock()

    embedder = SimpleNamespace(model_version="bge-m3", dim=1024)
    await ensure_news_collection(fake_client, embedder)

    # Three create_payload_index calls: ingested_at_ts, feed_id, title
    assert fake_client.create_payload_index.call_count == 3

    title_call = next(
        c
        for c in fake_client.create_payload_index.call_args_list
        if c.kwargs.get("field_name") == "title"
    )
    schema = title_call.kwargs["field_schema"]
    # TextIndexParams type=text + MULTILINGUAL + lowercase
    assert schema.type == "text"
    # qdrant-client serializes TokenizerType enum as a string-valued enum;
    # accept either the enum or its lowercase string form.
    tokenizer = getattr(schema, "tokenizer", None)
    assert tokenizer is not None
    tokenizer_str = tokenizer.value if hasattr(tokenizer, "value") else str(tokenizer)
    assert "multilingual" in tokenizer_str.lower()
    assert schema.lowercase is True
    assert schema.min_token_len == 1
    assert schema.max_token_len == 20


@pytest.mark.asyncio
async def test_ensure_news_collection_idempotent_when_collection_exists():
    """Second call must not re-create the collection or alias, but
    create_payload_index is still invoked (qdrant-client is itself idempotent
    server-side)."""
    fake_client = MagicMock()

    # Collection already exists → create_collection NOT called
    fake_client.get_collections = AsyncMock(
        return_value=SimpleNamespace(collections=[SimpleNamespace(name="news_bge-m3")])
    )
    fake_client.create_collection = AsyncMock()
    fake_client.create_payload_index = AsyncMock()
    fake_client.get_aliases = AsyncMock(
        return_value=SimpleNamespace(
            aliases=[SimpleNamespace(alias_name="news_current", collection_name="news_bge-m3")]
        )
    )
    fake_client.update_collection_aliases = AsyncMock()

    embedder = SimpleNamespace(model_version="bge-m3", dim=1024)
    await ensure_news_collection(fake_client, embedder)

    fake_client.create_collection.assert_not_called()
    fake_client.update_collection_aliases.assert_not_called()
    # All three payload indexes still attempted (no-op server-side if existing)
    assert fake_client.create_payload_index.call_count == 3

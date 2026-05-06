"""Unit tests for the embedding engine (Windows-runnable, no Docker/GPU deps).

All DB tests use in-memory SQLite. Embedder and Qdrant calls are mocked.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import httpx
import pytest
import respx

from sembr.collector.base import RawArticle
from sembr.db.articles import (
    PendingRow,
    _BODY_CAP_BYTES,
    delete_pending,
    demote_md5s_to_dead,
    demote_to_dead,
    increment_retry,
    init_article_tables,
    insert_article_pending,
    pull_pending_batch,
)
from sembr.db.feeds import init_feed_tables
from sembr.embedder.scheduler import (
    ALIAS_NAME,
    BATCH_SIZE,
    MAX_ATTEMPTS,
    _md5_to_uuid,
    add_embedder_worker_job,
    embedder_worker,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _make_conn() -> aiosqlite.Connection:
    """In-memory DB with all tables + foreign_keys=ON.

    Also registers the connection as the transaction() singleton so that
    articles.py functions (which call transaction() internally) operate on
    the same in-memory DB as the rest of the test.
    """
    import asyncio as _asyncio
    from sembr.db import sqlite as _sqlite_mod

    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_feed_tables(conn)
    await init_article_tables(conn)
    _sqlite_mod._conn = conn
    _sqlite_mod._WRITE_LOCK = _asyncio.Lock()
    return conn


async def _insert_feed(conn: aiosqlite.Connection) -> int:
    await conn.execute(
        "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t.com', 30)"
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
        return (await cur.fetchone())[0]


def _make_article(md5: str = "a" * 32, body: str = "body text") -> RawArticle:
    return RawArticle(
        url="https://example.com/art",
        title="Test Title",
        body=body,
        content_quality="summary",
        published_at=datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc),
        feed_md5=md5,
    )


# ---------------------------------------------------------------------------
# init_article_tables
# ---------------------------------------------------------------------------

async def test_init_article_tables_idempotent():
    conn = await _make_conn()
    await init_article_tables(conn)  # second call must not raise
    await conn.close()


# ---------------------------------------------------------------------------
# insert_article_pending
# ---------------------------------------------------------------------------

async def test_insert_article_pending_atomic_new():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    article = _make_article()

    result = await insert_article_pending(conn, article, feed_id)

    assert result is True
    async with conn.execute("SELECT COUNT(*) FROM feed_items WHERE md5=?", (article.feed_md5,)) as cur:
        assert (await cur.fetchone())[0] == 1
    async with conn.execute("SELECT COUNT(*) FROM pending_articles WHERE md5=?", (article.feed_md5,)) as cur:
        assert (await cur.fetchone())[0] == 1
    await conn.close()


async def test_insert_article_pending_dedup():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    article = _make_article()

    first = await insert_article_pending(conn, article, feed_id)
    second = await insert_article_pending(conn, article, feed_id)

    assert first is True
    assert second is False
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        assert (await cur.fetchone())[0] == 1
    await conn.close()


async def test_insert_article_pending_body_cap():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    big_body = "x" * (int(_BODY_CAP_BYTES * 1.5))
    article = _make_article(body=big_body)

    await insert_article_pending(conn, article, feed_id)

    async with conn.execute("SELECT length(body) FROM pending_articles") as cur:
        stored_len = (await cur.fetchone())[0]
    assert stored_len == _BODY_CAP_BYTES
    await conn.close()


# ---------------------------------------------------------------------------
# pull_pending_batch
# ---------------------------------------------------------------------------

async def test_pull_pending_batch_skips_max_retry():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)

    md5_dead = "b" * 32
    md5_live = "c" * 32
    for md5 in (md5_dead, md5_live):
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body, retry_count) "
        "VALUES (?, ?, 'u1', 't1', 'b1', ?)",
        (md5_dead, feed_id, MAX_ATTEMPTS),
    )
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body, retry_count) "
        "VALUES (?, ?, 'u2', 't2', 'b2', 0)",
        (md5_live, feed_id),
    )
    await conn.commit()

    batch = await pull_pending_batch(conn, BATCH_SIZE, MAX_ATTEMPTS)
    assert len(batch) == 1
    assert batch[0].md5 == md5_live
    await conn.close()


async def test_pull_pending_batch_order_by_insertion():
    """Rows come out in insertion (rowid) order, not md5 alphabetical order."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)

    ordered_md5s = ["f" * 32, "1" * 32, "a" * 32]
    for md5 in ordered_md5s:
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
        await conn.execute(
            "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'b')",
            (md5, feed_id),
        )
    await conn.commit()

    batch = await pull_pending_batch(conn, BATCH_SIZE, MAX_ATTEMPTS)
    assert [r.md5 for r in batch] == ordered_md5s
    await conn.close()


# ---------------------------------------------------------------------------
# demote_to_dead
# ---------------------------------------------------------------------------

async def test_demote_to_dead_atomic():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "d" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body, retry_count) "
        "VALUES (?, ?, 'u', 't', 'b', ?)",
        (md5, feed_id, MAX_ATTEMPTS),
    )
    await conn.commit()

    demoted = await demote_to_dead(conn, MAX_ATTEMPTS, error_message="test error")

    assert demoted == 1
    async with conn.execute("SELECT COUNT(*) FROM dead_articles WHERE md5=?", (md5,)) as cur:
        assert (await cur.fetchone())[0] == 1
    async with conn.execute("SELECT COUNT(*) FROM pending_articles WHERE md5=?", (md5,)) as cur:
        assert (await cur.fetchone())[0] == 0
    await conn.close()


async def test_increment_retry_then_demote():
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "e" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'b')",
        (md5, feed_id),
    )
    await conn.commit()

    for i in range(MAX_ATTEMPTS - 1):
        await increment_retry(conn, [md5])
    await increment_retry(conn, [md5])
    await demote_md5s_to_dead(conn, [md5], error_message="final error")

    async with conn.execute("SELECT COUNT(*) FROM dead_articles") as cur:
        assert (await cur.fetchone())[0] == 1
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        assert (await cur.fetchone())[0] == 0
    await conn.close()


async def test_demote_md5s_preserves_error_attribution():
    """Each batch's md5s are demoted with THAT batch's error, not a global message."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5_a = "a" * 32
    md5_b = "b" * 32

    for md5 in (md5_a, md5_b):
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
        await conn.execute(
            "INSERT INTO pending_articles (md5, feed_id, url, title, body, retry_count) "
            "VALUES (?, ?, 'u', 't', 'b', ?)",
            (md5, feed_id, MAX_ATTEMPTS),
        )
    await conn.commit()

    await demote_md5s_to_dead(conn, [md5_a], error_message="error for A")
    await demote_md5s_to_dead(conn, [md5_b], error_message="error for B")

    async with conn.execute("SELECT md5, error_message FROM dead_articles ORDER BY md5") as cur:
        rows = {r[0]: r[1] for r in await cur.fetchall()}

    assert rows[md5_a] == "error for A"
    assert rows[md5_b] == "error for B"
    await conn.close()


# ---------------------------------------------------------------------------
# UUID / point ID
# ---------------------------------------------------------------------------

def test_uuid_from_md5_deterministic():
    md5 = "a" * 32
    uid1 = _md5_to_uuid(md5)
    uid2 = _md5_to_uuid(md5)
    assert uid1 == uid2
    uuid.UUID(uid1)
    assert _md5_to_uuid("b" * 32) != uid1


# ---------------------------------------------------------------------------
# SiliconFlowEmbedder
# ---------------------------------------------------------------------------

async def test_siliconflow_embedder_load_probe_ok():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})
        )
        embedder = SiliconFlowEmbedder(api_key="test-key")
        await embedder.load()

    assert embedder.status == "ok"
    assert embedder.is_loaded


async def test_siliconflow_embedder_load_probe_http_error():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(401, json={"error": "Unauthorized"})
        )
        embedder = SiliconFlowEmbedder(api_key="bad-key")
        await embedder.load()  # must not raise

    assert embedder.status == "error"
    assert not embedder.is_loaded


async def test_siliconflow_embedder_load_probe_network_error():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            side_effect=httpx.ConnectError("connection refused")
        )
        embedder = SiliconFlowEmbedder(api_key="test-key")
        await embedder.load()  # must not raise

    assert embedder.status == "error"
    assert not embedder.is_loaded


async def test_siliconflow_embedder_aembed_returns_correct_shape():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    n = 4
    fake_embeddings = [[float(i)] * 1024 for i in range(n)]
    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(
                200, json={"data": [{"embedding": v} for v in fake_embeddings]}
            )
        )
        embedder = SiliconFlowEmbedder(api_key="test-key")
        embedder._for_testing_set_loaded()
        result = await embedder.aembed(["text"] * n)

    assert len(result) == n
    assert len(result[0]) == 1024


async def test_siliconflow_embedder_aembed_http_error_raises():
    from sembr.embedder.openai_compat import EmbedderAPIError, SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(429, json={"error": "rate limit"})
        )
        embedder = SiliconFlowEmbedder(api_key="test-key")
        embedder._for_testing_set_loaded()
        with pytest.raises(EmbedderAPIError):
            await embedder.aembed(["text"])


async def test_siliconflow_embedder_aembed_encoding_format_float_in_request():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    with respx.mock() as mock:
        route = mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})
        )
        embedder = SiliconFlowEmbedder(api_key="test-key", model="BAAI/bge-m3")
        embedder._for_testing_set_loaded()
        await embedder.aembed(["test text"])

    body = json.loads(route.calls.last.request.content)
    assert body["encoding_format"] == "float"
    assert body["model"] == "BAAI/bge-m3"


async def test_siliconflow_embedder_aembed_uses_secret_value():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    with respx.mock() as mock:
        route = mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})
        )
        embedder = SiliconFlowEmbedder(api_key="my-secret-key")
        embedder._for_testing_set_loaded()
        await embedder.aembed(["text"])

    auth = route.calls.last.request.headers.get("authorization", "")
    assert auth == "Bearer my-secret-key"


def test_siliconflow_embedder_model_version_string():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    embedder = SiliconFlowEmbedder(api_key="k")
    assert embedder.model_version == "bge-m3_v1"


def test_siliconflow_embedder_max_input_chars():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    embedder = SiliconFlowEmbedder(api_key="k")
    assert embedder.max_input_chars == 8_000


async def test_siliconflow_embedder_aembed_empty_list_returns_empty():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    embedder = SiliconFlowEmbedder(api_key="k")
    embedder._for_testing_set_loaded()
    result = await embedder.aembed([])
    assert result == []


async def test_siliconflow_embedder_aembed_empty_list_not_loaded_raises():
    """aembed([]) on a not-loaded embedder must raise, not silently return []."""
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    embedder = SiliconFlowEmbedder(api_key="k")
    assert embedder._status == "loading"
    with pytest.raises(RuntimeError, match="embedder not loaded"):
        await embedder.aembed([])


def test_siliconflow_embedder_uses_configured_timeout():
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    e = SiliconFlowEmbedder(api_key="k", timeout=7.5)
    client = httpx.AsyncClient(timeout=e._timeout)
    assert client.timeout.read == 7.5


async def test_siliconflow_embedder_aembed_empty_data_field_raises():
    """API returns 200 with empty data list → EmbedderAPIError."""
    from sembr.embedder.openai_compat import EmbedderAPIError, SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        embedder = SiliconFlowEmbedder(api_key="k")
        embedder._for_testing_set_loaded()
        with pytest.raises(EmbedderAPIError, match="length mismatch"):
            await embedder.aembed(["text"])


async def test_siliconflow_embedder_aembed_non_list_embedding_raises():
    """API returns base64 string instead of float list → EmbedderAPIError."""
    from sembr.embedder.openai_compat import EmbedderAPIError, SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"data": [{"embedding": "base64encodedstring"}]})
        )
        embedder = SiliconFlowEmbedder(api_key="k")
        embedder._for_testing_set_loaded()
        with pytest.raises(EmbedderAPIError, match="unexpected embedding shape"):
            await embedder.aembed(["text"])


async def test_siliconflow_embedder_aembed_missing_data_field_raises():
    """API returns 200 without 'data' key → EmbedderAPIError."""
    from sembr.embedder.openai_compat import EmbedderAPIError, SiliconFlowEmbedder

    with respx.mock() as mock:
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(200, json={"error": {"code": "invalid_input"}})
        )
        embedder = SiliconFlowEmbedder(api_key="k")
        embedder._for_testing_set_loaded()
        with pytest.raises(EmbedderAPIError, match="missing 'data' list"):
            await embedder.aembed(["text"])


async def test_siliconflow_embedder_api_key_redacted_in_error():
    """API key must not appear in EmbedderAPIError message."""
    from sembr.embedder.openai_compat import EmbedderAPIError, SiliconFlowEmbedder

    secret_key = "super-secret-token-xyz"
    with respx.mock() as mock:
        # Simulate a proxy that echoes the bearer token in error body
        mock.post("https://api.siliconflow.cn/v1/embeddings").mock(
            return_value=httpx.Response(
                401, text=f"Unauthorized: Bearer {secret_key} is invalid"
            )
        )
        embedder = SiliconFlowEmbedder(api_key=secret_key)
        embedder._for_testing_set_loaded()
        with pytest.raises(EmbedderAPIError) as exc_info:
            await embedder.aembed(["text"])

    assert secret_key not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_build_embedder_whitespace_api_key_raises():
    """Whitespace-only key must be rejected the same as empty key."""
    from sembr.config import Settings
    from sembr.embedder.factory import build_embedder

    settings = Settings(embedder_api_key="   ")
    with pytest.raises(ValueError, match="EMBEDDER_API_KEY"):
        build_embedder(settings)


# ---------------------------------------------------------------------------
# build_embedder factory
# ---------------------------------------------------------------------------

def test_build_embedder_returns_siliconflow():
    from sembr.config import Settings
    from sembr.embedder.factory import build_embedder
    from sembr.embedder.openai_compat import SiliconFlowEmbedder

    settings = Settings(embedder_backend="siliconflow", embedder_api_key="real-key")
    embedder = build_embedder(settings)
    assert isinstance(embedder, SiliconFlowEmbedder)


def test_build_embedder_missing_api_key_raises():
    from sembr.config import Settings
    from sembr.embedder.factory import build_embedder

    settings = Settings(embedder_api_key="")
    with pytest.raises(ValueError, match="EMBEDDER_API_KEY"):
        build_embedder(settings)


# ---------------------------------------------------------------------------
# embedder_worker
# ---------------------------------------------------------------------------

def _mock_embedder(is_loaded: bool = True, model_version: str = "bge-m3_v1") -> MagicMock:
    e = MagicMock()
    e.is_loaded = is_loaded
    e.model_version = model_version
    e.max_input_chars = 8_000
    e.aembed = AsyncMock(return_value=[[0.1] * 1024])
    return e


def _mock_qdrant() -> MagicMock:
    q = MagicMock()
    q.client.upsert = AsyncMock()
    return q


async def test_embedder_worker_skip_when_not_loaded():
    embedder = _mock_embedder(is_loaded=False)
    qdrant = _mock_qdrant()

    await embedder_worker(embedder, qdrant)

    embedder.aembed.assert_not_called()
    qdrant.client.upsert.assert_not_called()


async def test_embedder_worker_phase3b_upsert_then_delete(monkeypatch):
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "a" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'body')",
        (md5, feed_id),
    )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    import sembr.embedder.scheduler as _sched_mod
    call_order: list[str] = []
    _real_delete = _sched_mod.delete_pending

    async def _tracking_delete(conn, md5s):
        call_order.append("delete")
        return await _real_delete(conn, md5s)

    async def _tracking_upsert(**kwargs):
        call_order.append("upsert")

    qdrant.client.upsert = AsyncMock(side_effect=_tracking_upsert)
    monkeypatch.setattr("sembr.embedder.scheduler.delete_pending", _tracking_delete)

    await embedder_worker(embedder, qdrant)

    assert call_order == ["upsert", "delete"], f"D2 violation: expected upsert→delete, got {call_order}"
    qdrant.client.upsert.assert_called_once()
    assert qdrant.client.upsert.call_args.kwargs["collection_name"] == ALIAS_NAME

    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        assert (await cur.fetchone())[0] == 0

    await conn.close()


async def test_embedder_worker_qdrant_transient_no_retry_inc(monkeypatch):
    """ConnectError from Qdrant must not increment retry_count (D20)."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "b" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'body')",
        (md5, feed_id),
    )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    qdrant = _mock_qdrant()
    qdrant.client.upsert = AsyncMock(side_effect=httpx.ConnectError("refused"))

    await embedder_worker(embedder, qdrant)

    async with conn.execute("SELECT retry_count FROM pending_articles WHERE md5=?", (md5,)) as cur:
        retry_count = (await cur.fetchone())[0]
    assert retry_count == 0

    await conn.close()


async def test_embedder_worker_embed_exception_increments_retry(monkeypatch):
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "c" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'body')",
        (md5, feed_id),
    )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    embedder.aembed = AsyncMock(side_effect=RuntimeError("CUDA OOM"))
    qdrant = _mock_qdrant()

    await embedder_worker(embedder, qdrant)

    async with conn.execute("SELECT retry_count FROM pending_articles WHERE md5=?", (md5,)) as cur:
        retry_count = (await cur.fetchone())[0]
    assert retry_count == 1
    qdrant.client.upsert.assert_not_called()

    await conn.close()


async def test_embedder_worker_api_failure_increments_retry(monkeypatch):
    """EmbedderAPIError from SiliconFlow must increment retry_count via except Exception."""
    from sembr.embedder.openai_compat import EmbedderAPIError

    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "a1" * 16

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body) VALUES (?, ?, 'u', 't', 'body')",
        (md5, feed_id),
    )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    embedder.aembed = AsyncMock(side_effect=EmbedderAPIError("429 rate limit"))
    qdrant = _mock_qdrant()

    await embedder_worker(embedder, qdrant)

    async with conn.execute("SELECT retry_count FROM pending_articles WHERE md5=?", (md5,)) as cur:
        retry_count = (await cur.fetchone())[0]
    assert retry_count == 1
    qdrant.client.upsert.assert_not_called()

    await conn.close()


async def test_embedder_worker_demotes_only_exhausted_rows(monkeypatch):
    """Worker demotes only the rows from the current batch that just hit MAX_ATTEMPTS (🔴-2)."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5_exhausted = "e" * 32
    md5_young = "f" * 32

    for md5, rc in [(md5_exhausted, MAX_ATTEMPTS - 1), (md5_young, 0)]:
        await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
        await conn.execute(
            "INSERT INTO pending_articles (md5, feed_id, url, title, body, retry_count) "
            "VALUES (?, ?, 'u', 't', 'body', ?)",
            (md5, feed_id, rc),
        )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    embedder.aembed = AsyncMock(side_effect=RuntimeError("embed error"))
    qdrant = _mock_qdrant()

    await embedder_worker(embedder, qdrant)

    async with conn.execute("SELECT COUNT(*) FROM dead_articles WHERE md5=?", (md5_exhausted,)) as cur:
        assert (await cur.fetchone())[0] == 1

    async with conn.execute("SELECT retry_count FROM pending_articles WHERE md5=?", (md5_young,)) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1

    await conn.close()


async def test_embedder_worker_payload_fields(monkeypatch):
    """Qdrant point payload must include all required fields."""
    conn = await _make_conn()
    feed_id = await _insert_feed(conn)
    md5 = "d" * 32

    await conn.execute("INSERT INTO feed_items (md5, feed_id) VALUES (?, ?)", (md5, feed_id))
    await conn.execute(
        "INSERT INTO pending_articles (md5, feed_id, url, title, body, published_at) "
        "VALUES (?, ?, 'https://u.com', 'MyTitle', 'MyBody', '2026-04-27T12:00:00+00:00')",
        (md5, feed_id),
    )
    await conn.commit()

    monkeypatch.setattr("sembr.embedder.scheduler.get_conn", lambda: conn)

    embedder = _mock_embedder()
    qdrant = _mock_qdrant()

    await embedder_worker(embedder, qdrant)

    call_kwargs = qdrant.client.upsert.call_args.kwargs
    points = call_kwargs["points"]
    assert len(points) == 1
    payload = points[0].payload
    assert payload["url"] == "https://u.com"
    assert payload["title"] == "MyTitle"
    assert payload["body"] == "MyBody"
    assert payload["embedding_model_version"] == "bge-m3_v1"
    assert payload["feed_id"] == feed_id

    await conn.close()


def test_embedder_worker_no_more_wait_for_wrap():
    """Static check: _EMBED_TIMEOUT and asyncio.wait_for removed from scheduler.py."""
    source = pathlib.Path(__file__).parent.parent / "sembr" / "embedder" / "scheduler.py"
    content = source.read_text(encoding="utf-8")
    assert "_EMBED_TIMEOUT" not in content, "_EMBED_TIMEOUT should have been removed from scheduler.py"
    assert "asyncio.wait_for" not in content, "asyncio.wait_for should have been removed from scheduler.py"


# ---------------------------------------------------------------------------
# ensure_news_collection
# ---------------------------------------------------------------------------

async def test_ensure_news_collection_idempotent():
    import sys

    from sembr.vector_store.news import COLLECTION_NAME, ensure_news_collection

    mock_client = AsyncMock()

    collections_resp = MagicMock()
    collections_resp.collections = []
    mock_client.get_collections = AsyncMock(return_value=collections_resp)

    aliases_resp = MagicMock()
    aliases_resp.aliases = []
    mock_client.get_aliases = AsyncMock(return_value=aliases_resp)

    mock_client.create_collection = AsyncMock()
    mock_client.update_collection_aliases = AsyncMock()

    mock_qdrant_models = MagicMock()
    with patch.dict(
        sys.modules,
        {"qdrant_client": MagicMock(), "qdrant_client.models": mock_qdrant_models},
    ):
        await ensure_news_collection(mock_client)

        mock_client.create_collection.assert_called_once()
        mock_client.update_collection_aliases.assert_called_once()

        col = MagicMock()
        col.name = COLLECTION_NAME
        collections_resp2 = MagicMock()
        collections_resp2.collections = [col]
        mock_client.get_collections = AsyncMock(return_value=collections_resp2)

        alias = MagicMock()
        alias.alias_name = "news_current"
        alias.collection_name = COLLECTION_NAME
        aliases_resp2 = MagicMock()
        aliases_resp2.aliases = [alias]
        mock_client.get_aliases = AsyncMock(return_value=aliases_resp2)

        mock_client.create_collection.reset_mock()
        mock_client.update_collection_aliases.reset_mock()

        await ensure_news_collection(mock_client)

    mock_client.create_collection.assert_not_called()
    mock_client.update_collection_aliases.assert_not_called()


# ---------------------------------------------------------------------------
# /health endpoint
# ---------------------------------------------------------------------------

async def test_health_returns_503_during_loading():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.health import router

    app = FastAPI()
    app.include_router(router)

    mock_qdrant = MagicMock()
    mock_qdrant.ping = AsyncMock(return_value=True)
    app.state.qdrant = mock_qdrant

    mock_embedder = MagicMock()
    mock_embedder.status = "loading"
    app.state.embedder = mock_embedder

    with patch("sembr.api.health._sqlite_ok", new=AsyncMock(return_value=True)):
        with TestClient(app) as client:
            resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["embedder"] == "loading"
    assert body["status"] == "degraded"


async def test_health_returns_200_when_all_ok():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.health import router

    app = FastAPI()
    app.include_router(router)

    mock_qdrant = MagicMock()
    mock_qdrant.ping = AsyncMock(return_value=True)
    app.state.qdrant = mock_qdrant

    mock_embedder = MagicMock()
    mock_embedder.status = "ok"
    app.state.embedder = mock_embedder

    with patch("sembr.api.health._sqlite_ok", new=AsyncMock(return_value=True)):
        with TestClient(app) as client:
            resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["components"]["embedder"] == "ok"
    assert body["status"] == "ok"


async def test_health_returns_503_on_embedder_error():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.health import router

    app = FastAPI()
    app.include_router(router)

    mock_qdrant = MagicMock()
    mock_qdrant.ping = AsyncMock(return_value=True)
    app.state.qdrant = mock_qdrant

    mock_embedder = MagicMock()
    mock_embedder.status = "error"
    app.state.embedder = mock_embedder

    with patch("sembr.api.health._sqlite_ok", new=AsyncMock(return_value=True)):
        with TestClient(app) as client:
            resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["components"]["embedder"] == "error"

"""Integration tests for the /intents API (Windows-runnable, no Docker/GPU deps).

Design:
  - Sync TestClient — no per-test event loop overhead
  - Fresh in-memory SQLite per test via FastAPI lifespan (same event loop, no cross-loop issues)
  - vector_store functions mocked at the sembr.api.intents import boundary so that
    qdrant_client (not installed on Windows dev machine) is never imported
  - Embedder mocked via AsyncMock
  - ensure_intents_collection tested separately by patching sys.modules (mirrors news collection test)
"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.intents import router
from sembr.db.intents import init_intent_tables

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAKE_VECTOR = [0.1] * 1024

VALID_BODY = {
    "name": "fed",
    "text": "Fed rate decisions impact on emerging markets",
    "channels": [{"type": "telegram", "config": {"chat_id": "1"}}],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_embedder(is_loaded: bool = True) -> MagicMock:
    e = MagicMock()
    e.is_loaded = is_loaded
    e.model_version = "bge-m3_v1"
    e.aembed = AsyncMock(return_value=[FAKE_VECTOR])
    return e


def _make_vs() -> dict[str, AsyncMock]:
    """Mock handles for the three vector_store functions the router calls."""
    return {
        "upsert": AsyncMock(),
        "update_payload": AsyncMock(),
        "delete": AsyncMock(),
    }


@contextmanager
def _client(embedder: MagicMock | None = None, vs: dict | None = None):
    """Yield (TestClient, vs_mocks) with a fresh in-memory SQLite DB.

    The DB is created inside the TestClient's event loop via a FastAPI lifespan
    so there is no cross-loop aiosqlite issue.  vector_store functions are
    patched at the sembr.api.intents boundary so qdrant_client is never imported.
    """
    if embedder is None:
        embedder = _make_embedder()
    if vs is None:
        vs = _make_vs()

    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.state.embedder = embedder
    app.state.qdrant = MagicMock()  # .client attribute accessed but never called directly

    with (
        patch("sembr.api.intents.get_conn", side_effect=lambda: conn_holder["conn"]),
        patch("sembr.api.intents.upsert_intent_point", vs["upsert"]),
        patch("sembr.api.intents.update_intent_payload", vs["update_payload"]),
        patch("sembr.api.intents.delete_intent_point", vs["delete"]),
    ):
        with TestClient(app) as http:
            yield http, vs


# ---------------------------------------------------------------------------
# Req #1 — POST creates intent: 201, upsert called with correct payload
# ---------------------------------------------------------------------------


def test_post_intent_success() -> None:
    with _client() as (http, vs):
        resp = http.post("/intents", json=VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert "id" in data
    assert data["name"] == VALID_BODY["name"]
    assert data["enabled"] is True

    vs["upsert"].assert_called_once()
    _intent_id, vector, payload = (
        vs["upsert"].call_args.args[1],
        vs["upsert"].call_args.args[2],
        vs["upsert"].call_args.kwargs["payload"],
    )
    assert _intent_id == data["id"]
    assert len(vector) == 1024
    assert payload["enabled"] is True
    assert payload["embedding_model_version"] == "bge-m3_v1"
    assert payload["intent_id"] == data["id"]


# ---------------------------------------------------------------------------
# Req #2 — embedder loading → POST 503, zero side-effects
# ---------------------------------------------------------------------------


def test_post_intent_503_when_embedder_loading() -> None:
    vs = _make_vs()
    with _client(embedder=_make_embedder(is_loaded=False), vs=vs) as (http, _):
        resp = http.post("/intents", json=VALID_BODY)
        assert resp.status_code == 503
        assert http.get("/intents").json() == []  # no row inserted

    vs["upsert"].assert_not_called()


# ---------------------------------------------------------------------------
# Req #3 — invalid request bodies → 422 (single test, looped — avoids 12× event loops)
# ---------------------------------------------------------------------------

_INVALID_BODIES = [
    # missing required fields
    {"text": "ok", "channels": [{"type": "telegram", "config": {}}]},
    {"name": "n", "channels": [{"type": "telegram", "config": {}}]},
    {"name": "n", "text": "ok"},
    # channels constraints
    {"name": "n", "text": "ok", "channels": []},
    {"name": "n", "text": "ok", "channels": [{"type": "slack", "config": {}}]},
    # threshold out of range (boundaries: valid=0.60–0.95)
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 0.5},
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 0.59},
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 1.0},
    # name / text empty or too long
    {"name": "", "text": "ok", "channels": [{"type": "telegram", "config": {}}]},
    {"name": "x" * 101, "text": "ok", "channels": [{"type": "telegram", "config": {}}]},
    {"name": "n", "text": "", "channels": [{"type": "telegram", "config": {}}]},
    {"name": "n", "text": "x" * 2001, "channels": [{"type": "telegram", "config": {}}]},
    # tags constraints
    {
        "name": "n",
        "text": "ok",
        "channels": [{"type": "telegram", "config": {}}],
        "tags": ["x" * 51],
    },
    {
        "name": "n",
        "text": "ok",
        "channels": [{"type": "telegram", "config": {}}],
        "tags": [f"t{i}" for i in range(11)],
    },
    # channels.config must be a dict, not a scalar (M5)
    {"name": "n", "text": "ok", "channels": [{"type": "telegram", "config": "not-a-dict"}]},
]


def test_post_intent_validation_errors() -> None:
    with _client() as (http, _):
        for bad_body in _INVALID_BODIES:
            resp = http.post("/intents", json=bad_body)
            assert resp.status_code == 422, f"expected 422 for body: {bad_body}"


# ---------------------------------------------------------------------------
# Req #4 + #5 — GET list returns all; GET by id 404 on missing
# ---------------------------------------------------------------------------


def test_get_intents_list_and_404() -> None:
    with _client() as (http, _):
        for _ in range(3):
            http.post("/intents", json=VALID_BODY)

        assert len(http.get("/intents").json()) == 3
        assert http.get("/intents/9999").status_code == 404


# ---------------------------------------------------------------------------
# Req #6 — PUT non-text field: no re-embed, update_payload called
# ---------------------------------------------------------------------------


def test_put_non_text_field_no_reembed() -> None:
    embedder = _make_embedder()
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]
        embedder.aembed.reset_mock()
        vs["upsert"].reset_mock()

        resp = http.put(f"/intents/{intent_id}", json={"threshold": 0.9})

    assert resp.status_code == 200
    assert resp.json()["threshold"] == 0.9
    embedder.aembed.assert_not_called()
    vs["upsert"].assert_not_called()
    vs["update_payload"].assert_called_once()
    assert vs["update_payload"].call_args.kwargs["payload"]["threshold"] == 0.9


# ---------------------------------------------------------------------------
# Req #7 — PUT text field: aembed called with new text, upsert called
# ---------------------------------------------------------------------------


def test_put_text_field_reembeds() -> None:
    embedder = _make_embedder()
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]
        embedder.aembed.reset_mock()
        vs["upsert"].reset_mock()

        resp = http.put(f"/intents/{intent_id}", json={"text": "new monitor topic"})

    assert resp.status_code == 200
    assert resp.json()["text"] == "new monitor topic"
    embedder.aembed.assert_called_once_with(["new monitor topic"])
    vs["upsert"].assert_called_once()
    assert vs["upsert"].call_args.args[1] == intent_id  # point_id == SQLite id (D2)


# ---------------------------------------------------------------------------
# Req #8 — embedder loading + PUT text → 503, SQLite text unchanged
# ---------------------------------------------------------------------------


def test_put_text_503_when_embedder_loading() -> None:
    embedder = _make_embedder(is_loaded=True)
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        data = http.post("/intents", json=VALID_BODY).json()
        intent_id, original_text = data["id"], data["text"]
        upsert_before = vs["upsert"].call_count

        embedder.is_loaded = False
        resp = http.put(f"/intents/{intent_id}", json={"text": "changed text"})

        current_text = http.get(f"/intents/{intent_id}").json()["text"]

    assert resp.status_code == 503
    assert current_text == original_text
    assert vs["upsert"].call_count == upsert_before


# ---------------------------------------------------------------------------
# Req #9 — DELETE: 204, delete_point called, row gone, GET 404
# ---------------------------------------------------------------------------


def test_delete_intent() -> None:
    vs = _make_vs()

    with _client(vs=vs) as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]

        assert http.delete(f"/intents/{intent_id}").status_code == 204
        assert http.get(f"/intents/{intent_id}").status_code == 404

    vs["delete"].assert_called_once()
    assert vs["delete"].call_args.args[1] == intent_id  # correct point deleted


# ---------------------------------------------------------------------------
# Req #10 — DELETE non-existent → 404, delete_point not called
# ---------------------------------------------------------------------------


def test_delete_nonexistent_intent() -> None:
    vs = _make_vs()

    with _client(vs=vs) as (http, _):
        assert http.delete("/intents/9999").status_code == 404

    vs["delete"].assert_not_called()


# ---------------------------------------------------------------------------
# Req #11 — enabled toggle: update_payload only, upsert never re-called
# ---------------------------------------------------------------------------


def test_enabled_toggle_no_vector_change() -> None:
    embedder = _make_embedder()
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]
        upsert_after_create = vs["upsert"].call_count  # == 1

        resp_off = http.put(f"/intents/{intent_id}", json={"enabled": False})
        assert resp_off.status_code == 200
        assert resp_off.json()["enabled"] is False

        resp_on = http.put(f"/intents/{intent_id}", json={"enabled": True})
        assert resp_on.status_code == 200
        assert resp_on.json()["enabled"] is True

    assert vs["upsert"].call_count == upsert_after_create  # no re-upsert after create
    assert vs["update_payload"].call_count == 2             # one per toggle

    calls = vs["update_payload"].call_args_list
    assert calls[0].kwargs["payload"]["enabled"] is False
    assert calls[1].kwargs["payload"]["enabled"] is True


# ---------------------------------------------------------------------------
# I5 — POST rollback: upsert failure → 500 + SQLite row absent
# ---------------------------------------------------------------------------


def test_post_intent_rollback_on_upsert_failure() -> None:
    vs = _make_vs()
    vs["upsert"].side_effect = RuntimeError("qdrant boom")

    with _client(vs=vs) as (http, _):
        resp = http.post("/intents", json=VALID_BODY)
        assert resp.status_code == 500
        assert http.get("/intents").json() == []  # SQLite row rolled back


# ---------------------------------------------------------------------------
# I5 — PUT rollback: update_payload failure → 500 + SQLite threshold restored
# ---------------------------------------------------------------------------


def test_put_intent_rollback_on_qdrant_failure() -> None:
    embedder = _make_embedder()
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        data = http.post("/intents", json=VALID_BODY).json()
        intent_id, original_threshold = data["id"], data["threshold"]

        vs["update_payload"].side_effect = RuntimeError("qdrant boom")
        resp = http.put(f"/intents/{intent_id}", json={"threshold": 0.9})

        assert resp.status_code == 500
        # SQLite must be rolled back to the snapshot
        assert http.get(f"/intents/{intent_id}").json()["threshold"] == original_threshold


# ---------------------------------------------------------------------------
# L2-QA-1 — PUT text-change rollback: upsert fails after SQLite write → 500 + text restored
# ---------------------------------------------------------------------------


def test_put_text_change_rollback_on_upsert_failure() -> None:
    embedder = _make_embedder()
    vs = _make_vs()

    with _client(embedder=embedder, vs=vs) as (http, _):
        data = http.post("/intents", json=VALID_BODY).json()
        intent_id, original_text = data["id"], data["text"]

        vs["upsert"].side_effect = RuntimeError("qdrant boom on re-embed")
        resp = http.put(f"/intents/{intent_id}", json={"text": "new text that will fail"})

        assert resp.status_code == 500
        current_text = http.get(f"/intents/{intent_id}").json()["text"]

    assert current_text == original_text  # SQLite must be rolled back to original


# ---------------------------------------------------------------------------
# I4 — ensure_intents_collection: collection config (D3) and alias (D4) verified
# ---------------------------------------------------------------------------


async def test_ensure_intents_collection_creates_with_correct_config() -> None:
    from sembr.vector_store.intents import (  # noqa: PLC0415
        ALIAS_NAME,
        COLLECTION_NAME,
        ensure_intents_collection,
    )

    mock_client = AsyncMock()

    # Neither collection nor alias exist yet
    collections_resp = MagicMock()
    collections_resp.collections = []
    mock_client.get_collections = AsyncMock(return_value=collections_resp)

    aliases_resp = MagicMock()
    aliases_resp.aliases = []
    mock_client.get_aliases = AsyncMock(return_value=aliases_resp)
    mock_client.create_collection = AsyncMock()
    mock_client.update_collection_aliases = AsyncMock()

    mock_qdrant_models = MagicMock()
    with patch.dict(sys.modules, {"qdrant_client": MagicMock(), "qdrant_client.models": mock_qdrant_models}):
        await ensure_intents_collection(mock_client)

        # D3: size=1024, distance=COSINE, on_disk=False, no quantization_config
        mock_qdrant_models.VectorParams.assert_called_once_with(
            size=1024,
            distance=mock_qdrant_models.Distance.COSINE,
            on_disk=False,
        )
        create_kwargs = mock_client.create_collection.call_args.kwargs
        assert create_kwargs["collection_name"] == COLLECTION_NAME
        assert "quantization_config" not in create_kwargs  # O2-B: no quantization

        # D4: alias intents_current → intents_bge-m3_v1
        mock_client.update_collection_aliases.assert_called_once()

        # Idempotency: second call with collection + alias already present must be no-op
        col = MagicMock()
        col.name = COLLECTION_NAME
        collections_resp2 = MagicMock()
        collections_resp2.collections = [col]
        mock_client.get_collections = AsyncMock(return_value=collections_resp2)

        alias = MagicMock()
        alias.alias_name = ALIAS_NAME
        alias.collection_name = COLLECTION_NAME
        aliases_resp2 = MagicMock()
        aliases_resp2.aliases = [alias]
        mock_client.get_aliases = AsyncMock(return_value=aliases_resp2)

        mock_client.create_collection.reset_mock()
        mock_client.update_collection_aliases.reset_mock()

        await ensure_intents_collection(mock_client)

    mock_client.create_collection.assert_not_called()
    mock_client.update_collection_aliases.assert_not_called()

# SPDX-License-Identifier: Apache-2.0
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
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.intents import router
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.db.sqlite import install_for_test

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAKE_VECTOR = [0.1] * 1024

VALID_BODY = {
    "name": "fed",
    "text": "Fed rate decisions impact on emerging markets",
    "channels": [{"type": "email", "to": ["a@example.com"]}],
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
    matcher.jobs functions are patched so no real APScheduler is needed.
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
        await init_match_seen_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.state.embedder = embedder
    app.state.qdrant = MagicMock()  # .client attribute accessed but never called directly
    app.state.scheduler = MagicMock()
    app.state.settings = MagicMock()

    project_prompts = Path(__file__).parent.parent / "prompts"
    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", project_prompts),
        patch("sembr.api.intents.get_conn", side_effect=lambda: conn_holder["conn"]),
        patch("sembr.api.intents.upsert_intent_point", vs["upsert"]),
        patch("sembr.api.intents.update_intent_payload", vs["update_payload"]),
        patch("sembr.api.intents.delete_intent_point", vs["delete"]),
        patch("sembr.api.intents.register_intent_job", MagicMock()),
        patch("sembr.api.intents.reregister_intent_job", MagicMock()),
        patch("sembr.api.intents.unregister_intent_job", MagicMock()),
        patch("sembr.api.intents.clear_intent", AsyncMock()),
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
    _intent_id, vectors, payload = (
        vs["upsert"].call_args.args[1],
        vs["upsert"].call_args.args[2],
        vs["upsert"].call_args.kwargs["payload"],
    )
    assert _intent_id == data["id"]
    # intent-match-enhancement: upsert_intent_point now takes a slot dict; no sub_texts → only "main".
    assert isinstance(vectors, dict)
    assert set(vectors.keys()) == {"main"}
    assert len(vectors["main"]) == 1024
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
    {"text": "ok", "channels": [{"type": "email", "to": ["a@example.com"]}]},
    {"name": "n", "channels": [{"type": "email", "to": ["a@example.com"]}]},
    {"name": "n", "text": "ok"},
    # channels constraints
    {"name": "n", "text": "ok", "channels": []},
    {"name": "n", "text": "ok", "channels": [{"type": "slack", "config": {}}]},
    # threshold out of range (boundaries: valid=0.60–0.95)
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 0.5},
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 0.59},
    {"name": "n", "text": "ok", "channels": [{"type": "email", "config": {}}], "threshold": 1.0},
    # name / text empty or too long
    {"name": "", "text": "ok", "channels": [{"type": "email", "to": ["a@example.com"]}]},
    {"name": "x" * 101, "text": "ok", "channels": [{"type": "email", "to": ["a@example.com"]}]},
    {"name": "n", "text": "", "channels": [{"type": "email", "to": ["a@example.com"]}]},
    {"name": "n", "text": "x" * 2001, "channels": [{"type": "email", "to": ["a@example.com"]}]},
    # tags constraints
    {
        "name": "n",
        "text": "ok",
        "channels": [{"type": "email", "to": ["a@example.com"]}],
        "tags": ["x" * 51],
    },
    {
        "name": "n",
        "text": "ok",
        "channels": [{"type": "email", "to": ["a@example.com"]}],
        "tags": [f"t{i}" for i in range(11)],
    },
    # email channel requires `to` as a non-empty list
    {"name": "n", "text": "ok", "channels": [{"type": "email"}]},
    # invalid email address is rejected at the API boundary (EmailStr)
    {"name": "n", "text": "ok", "channels": [{"type": "email", "to": ["not-an-email"]}]},
    # to must be a list, not a string
    {"name": "n", "text": "ok", "channels": [{"type": "email", "to": "a@example.com"}]},
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
    assert vs["upsert"].call_args.args[1] == intent_id  # point_id == SQLite id


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
    assert vs["update_payload"].call_count == 2  # one per toggle

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
# ensure_intents_collection: collection config + alias verified on fresh install
# ---------------------------------------------------------------------------


async def test_ensure_intents_collection_creates_with_correct_config() -> None:
    """Named-vector layout `_mv` collection on fresh install."""
    from sembr.vector_store.intents import (  # noqa: PLC0415
        ALIAS_NAME,
        ensure_intents_collection,
        multi_vec_collection_name,
    )

    expected_name = multi_vec_collection_name("bge-m3_v1")  # intents_bge-m3_v1_mv

    mock_embedder = MagicMock()
    mock_embedder.model_version = "bge-m3_v1"
    mock_embedder.dim = 1024

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
    with patch.dict(
        sys.modules, {"qdrant_client": MagicMock(), "qdrant_client.models": mock_qdrant_models}
    ):
        # conn=None — no SQLite rows to re-embed; fresh install branch
        await ensure_intents_collection(mock_client, mock_embedder, conn=None)

        # Named-vector layout: VectorParams should be called once per slot {main, alt_0, alt_1, alt_2}
        assert mock_qdrant_models.VectorParams.call_count == 4
        for call in mock_qdrant_models.VectorParams.call_args_list:
            assert call.kwargs["size"] == 1024
            assert call.kwargs["distance"] == mock_qdrant_models.Distance.COSINE
            assert call.kwargs["on_disk"] is False

        create_kwargs = mock_client.create_collection.call_args.kwargs
        assert create_kwargs["collection_name"] == expected_name  # _mv suffix
        # vectors_config should be a dict[slot_name → VectorParams]
        assert isinstance(create_kwargs["vectors_config"], dict)
        assert set(create_kwargs["vectors_config"].keys()) == {"main", "alt_0", "alt_1", "alt_2"}
        assert "quantization_config" not in create_kwargs  # no quantization for intents

        # Alias intents_current → _mv collection
        mock_client.update_collection_aliases.assert_called_once()

        # Idempotency: second call with named-vec collection + alias already in place → no-op
        col = MagicMock()
        col.name = expected_name
        collections_resp2 = MagicMock()
        collections_resp2.collections = [col]
        mock_client.get_collections = AsyncMock(return_value=collections_resp2)

        alias = MagicMock()
        alias.alias_name = ALIAS_NAME
        alias.collection_name = expected_name
        aliases_resp2 = MagicMock()
        aliases_resp2.aliases = [alias]
        mock_client.get_aliases = AsyncMock(return_value=aliases_resp2)

        # Layout probe: get_collection must return a named-vec dict layout
        col_info = MagicMock()
        col_info.config.params.vectors = {
            "main": MagicMock(),
            "alt_0": MagicMock(),
            "alt_1": MagicMock(),
            "alt_2": MagicMock(),
        }
        mock_client.get_collection = AsyncMock(return_value=col_info)

        mock_client.create_collection.reset_mock()
        mock_client.update_collection_aliases.reset_mock()

        await ensure_intents_collection(mock_client, mock_embedder, conn=None)

    mock_client.create_collection.assert_not_called()
    mock_client.update_collection_aliases.assert_not_called()


# ---------------------------------------------------------------------------
# SC#13 — new fields: range validation on schedule / lookback_window_seconds
# ---------------------------------------------------------------------------

_NEW_FIELD_INVALID_BODIES = [
    # EventSchedule trigger_count out of range
    {**VALID_BODY, "schedule": {"mode": "event", "trigger_count": 0}},  # below minimum
    {**VALID_BODY, "schedule": {"mode": "event", "trigger_count": 11}},  # above maximum
    # CronSchedule weekly without weekday
    {**VALID_BODY, "schedule": {"mode": "cron", "preset": "weekly", "hour": 9}},
    # EventSchedule max_wait_seconds out of range
    {**VALID_BODY, "schedule": {"mode": "event", "max_wait_seconds": 59}},  # below minimum
    {**VALID_BODY, "schedule": {"mode": "event", "max_wait_seconds": 86401}},  # above maximum
]


def test_post_intent_new_fields_validation() -> None:
    with _client() as (http, _):
        for bad_body in _NEW_FIELD_INVALID_BODIES:
            resp = http.post("/intents", json=bad_body)
            assert resp.status_code == 422, f"expected 422 for body: {bad_body}"


def test_post_intent_new_fields_defaults() -> None:
    """New scheduling fields have sensible defaults and appear in response."""
    with _client() as (http, _):
        resp = http.post("/intents", json=VALID_BODY)

    assert resp.status_code == 201
    data = resp.json()
    assert data["schedule"]["mode"] == "cron"
    assert data["schedule"]["preset"] == "daily"
    assert data["schedule"]["lookback_seconds"] == 86400
    assert data["schedule"]["skip_seen"] is True
    assert data["feed_filter"] is None
    assert data["timezone"] == "UTC"
    assert data["language"] == "zh"


def test_put_intent_schedule_fields() -> None:
    """PUT schedule updates the stored value."""
    with _client() as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]
        resp = http.put(
            f"/intents/{intent_id}", json={"schedule": {"mode": "cron", "preset": "hourly"}}
        )

    assert resp.status_code == 200
    assert resp.json()["schedule"]["mode"] == "cron"
    assert resp.json()["schedule"]["preset"] == "hourly"


def test_put_intent_schedule_invalid() -> None:
    """PUT with invalid schedule → 422."""
    with _client() as (http, _):
        intent_id = http.post("/intents", json=VALID_BODY).json()["id"]
        resp = http.put(
            f"/intents/{intent_id}", json={"schedule": {"mode": "cron", "preset": "weekly"}}
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Regression: PUT enabled+text combination must clear match_seen
# ---------------------------------------------------------------------------


def test_put_reenable_with_text_change_clears_match_seen() -> None:
    """Re-enabling a disabled intent with new text must clear match_seen.

    Regression test: when enabled_changed=True AND text_changed=True, clear_intent
    was skipped because the enabled_changed branch short-circuited the elif block
    containing clear_intent.
    """
    # Now test via the API: PUT {enabled: true, text: "new text"} on a disabled intent
    # must clear match_seen via the text-changed path even though enabled_changed is True.
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        import aiosqlite

        from sembr.db.intents import create_intent, init_intent_tables
        from sembr.db.match_seen import init_match_seen_tables, insert_unseen_returning_new
        from sembr.models import IntentCreate

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await init_match_seen_tables(conn)
        install_for_test(conn)
        intent = await create_intent(
            conn,
            IntentCreate(
                name="reenable-test",
                text="original text",
                enabled=False,
                channels=[{"type": "email", "to": ["a@example.com"]}],
            ),
        )
        await insert_unseen_returning_new(conn, intent.id, ["stale-1", "stale-2"])
        conn_holder["conn"] = conn
        conn_holder["intent_id"] = intent.id
        yield
        await conn.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.intents import router

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    embedder = MagicMock()
    embedder.is_loaded = True
    embedder.model_version = "bge-m3_v1"
    embedder.aembed = AsyncMock(return_value=[[0.1] * 1024])
    app.state.embedder = embedder
    app.state.qdrant = MagicMock()
    app.state.scheduler = MagicMock()

    with (
        patch("sembr.api.intents.get_conn", side_effect=lambda: conn_holder["conn"]),
        patch("sembr.api.intents.upsert_intent_point", AsyncMock()),
        patch("sembr.api.intents.update_intent_payload", AsyncMock()),
        patch("sembr.api.intents.delete_intent_point", AsyncMock()),
        patch("sembr.api.intents.register_intent_job", MagicMock()),
        patch("sembr.api.intents.reregister_intent_job", MagicMock()),
        patch("sembr.api.intents.unregister_intent_job", MagicMock()),
        patch("sembr.api.intents.clear_intent", AsyncMock()) as mock_clear,
    ):
        with TestClient(app) as http:
            iid = conn_holder["intent_id"]
            resp = http.put(f"/intents/{iid}", json={"enabled": True, "text": "new text"})
            assert resp.status_code == 200

    # clear_intent must have been called because text changed, even though
    # enabled_changed was also True (the old bug caused clear to be skipped here).
    mock_clear.assert_awaited_once()


def test_put_text_change_clear_intent_failure_not_silent() -> None:
    """clear_intent failure on text change must return 500, not silently succeed."""
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app):
        import aiosqlite

        from sembr.db.intents import create_intent, init_intent_tables
        from sembr.db.match_seen import init_match_seen_tables
        from sembr.models import IntentCreate

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await init_match_seen_tables(conn)
        install_for_test(conn)
        intent = await create_intent(
            conn,
            IntentCreate(
                name="clear-fail-test",
                text="original text",
                enabled=True,
                channels=[{"type": "email", "to": ["a@example.com"]}],
            ),
        )
        conn_holder["conn"] = conn
        conn_holder["intent_id"] = intent.id
        yield
        await conn.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.intents import router

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    embedder = MagicMock()
    embedder.is_loaded = True
    embedder.model_version = "bge-m3_v1"
    embedder.aembed = AsyncMock(return_value=[[0.1] * 1024])
    app.state.embedder = embedder
    app.state.qdrant = MagicMock()
    app.state.scheduler = MagicMock()

    with (
        patch("sembr.api.intents.get_conn", side_effect=lambda: conn_holder["conn"]),
        patch("sembr.api.intents.upsert_intent_point", AsyncMock()),
        patch("sembr.api.intents.update_intent_payload", AsyncMock()),
        patch("sembr.api.intents.delete_intent_point", AsyncMock()),
        patch("sembr.api.intents.register_intent_job", MagicMock()),
        patch("sembr.api.intents.reregister_intent_job", MagicMock()),
        patch("sembr.api.intents.unregister_intent_job", MagicMock()),
        patch(
            "sembr.api.intents.clear_intent",
            AsyncMock(side_effect=RuntimeError("db locked")),
        ),
    ):
        with TestClient(app) as http:
            iid = conn_holder["intent_id"]
            resp = http.put(f"/intents/{iid}", json={"text": "new text"})

    assert resp.status_code == 500
    assert "deduplication" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Loop 2 🔴-1: PUT sub_texts write must be atomic with intents row write
# ---------------------------------------------------------------------------


def test_put_sub_texts_split_brain_rolled_back() -> None:
    """🔴-1: when the sub_texts write fails inside the PUT transaction, the
    intents row must NOT be left with the new values. The single-transaction
    pattern guarantees both writes commit or neither does.
    """

    embedder = _make_embedder()
    vs = _make_vs()
    with _client(embedder=embedder, vs=vs) as (http, _):
        # Create an intent with no sub_texts; original text recorded.
        body = {**VALID_BODY, "text": "ORIGINAL TEXT", "sub_texts": []}
        resp = http.post("/intents", json=body)
        assert resp.status_code == 201
        iid = resp.json()["id"]

        # Make the child-table write fail mid-transaction.
        with patch(
            "sembr.api.intents._sub_texts_replace_in_txn",
            AsyncMock(side_effect=RuntimeError("simulated child-table failure")),
        ):
            put_resp = http.put(
                f"/intents/{iid}",
                json={
                    "text": "MODIFIED TEXT",
                    "sub_texts": [{"language": "en", "text": "english variant"}],
                },
            )

        # Backend returns 500 because the PUT transaction raised.
        assert put_resp.status_code == 500

        # Critical: GET must show the ORIGINAL intent text — the failed inner
        # write must have rolled back the outer UPDATE too. If the two writes
        # were independent transactions (Loop 1 bug), the intents row would
        # already be "MODIFIED TEXT" with empty sub_texts.
        get_resp = http.get(f"/intents/{iid}")
        assert get_resp.status_code == 200
        data = get_resp.json()
        assert data["text"] == "ORIGINAL TEXT", (
            "PUT split-brain regression: intents row updated despite sub_texts write failure"
        )
        assert data["sub_texts"] == []


# ---------------------------------------------------------------------------
# Partial-migration recovery detects ID divergence (not just count)
# ---------------------------------------------------------------------------


async def test_lifespan_recovery_id_mismatch() -> None:
    """🟡-1: when _mv collection has the right row count but the ID set has
    diverged from SQLite (e.g. user DELETE+POST between two failed migration
    attempts), ensure_intents_collection must detect the mismatch via scroll
    and recreate the collection — count alone is insufficient.
    """
    from sembr.vector_store.intents import (  # noqa: PLC0415
        ensure_intents_collection,
        multi_vec_collection_name,
    )

    mv_name = multi_vec_collection_name("bge-m3_v1")

    mock_embedder = MagicMock()
    mock_embedder.model_version = "bge-m3_v1"
    mock_embedder.dim = 1024
    mock_embedder.aembed = AsyncMock(return_value=[[0.1] * 1024, [0.2] * 1024, [0.3] * 1024])

    mock_client = AsyncMock()

    # _mv exists from a prior failed migration with IDs {1, 2, 5}
    col = MagicMock()
    col.name = mv_name
    collections_resp = MagicMock()
    collections_resp.collections = [col]
    mock_client.get_collections = AsyncMock(return_value=collections_resp)

    # Alias still points at the legacy unnamed-vec collection (or nothing)
    aliases_resp = MagicMock()
    aliases_resp.aliases = []
    mock_client.get_aliases = AsyncMock(return_value=aliases_resp)

    # Layout probe succeeds: _mv has named-vec layout
    col_info = MagicMock()
    col_info.config.params.vectors = {
        "main": MagicMock(),
        "alt_0": MagicMock(),
        "alt_1": MagicMock(),
        "alt_2": MagicMock(),
    }
    mock_client.get_collection = AsyncMock(return_value=col_info)

    # Count probe says 3 — same as SQLite (the count-only check would pass!)
    count_resp = MagicMock()
    count_resp.count = 3
    mock_client.count = AsyncMock(return_value=count_resp)

    # But scroll reveals IDs {1, 2, 5} — SQLite has {1, 2, 4}, so they diverge.
    scroll_pts = [MagicMock(id=1), MagicMock(id=2), MagicMock(id=5)]
    mock_client.scroll = AsyncMock(return_value=(scroll_pts, None))

    # Track recreate path
    mock_client.delete_collection = AsyncMock()
    mock_client.create_collection = AsyncMock()
    mock_client.upsert = AsyncMock()
    mock_client.update_collection_aliases = AsyncMock()

    # SQLite mock: SELECT id, text returns 3 rows but IDs are {1, 2, 4}
    mock_conn = AsyncMock()
    cursor = AsyncMock()
    cursor.fetchall = AsyncMock(return_value=[(1, "t1"), (2, "t2"), (4, "t4")])
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)
    mock_conn.execute = MagicMock(return_value=cursor)

    mock_qdrant_models = MagicMock()
    with patch.dict(
        sys.modules, {"qdrant_client": MagicMock(), "qdrant_client.models": mock_qdrant_models}
    ):
        await ensure_intents_collection(mock_client, mock_embedder, conn=mock_conn)

    # Critical assertion: delete_collection must have been called because the
    # ID set diverged, even though count matched (3 == 3).
    mock_client.delete_collection.assert_called_once_with(mv_name)
    # And then create_collection rebuilds the _mv collection from scratch.
    mock_client.create_collection.assert_called_once()
    # And upsert repopulates with the SQLite-truth rows.
    mock_client.upsert.assert_called_once()
    # Finally alias flip points to the freshly rebuilt _mv.
    mock_client.update_collection_aliases.assert_called_once()

# SPDX-License-Identifier: Apache-2.0
"""Tests for template existence validation on POST/PUT /intents."""

from __future__ import annotations

import tempfile
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.intents import router
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.db.sqlite import install_for_test

FAKE_VECTOR = [0.1] * 1024

VALID_BODY = {
    "name": "fed",
    "text": "Fed rate decisions",
    "channels": [{"type": "email", "to": ["a@example.com"]}],
}


def _make_embedder() -> MagicMock:
    e = MagicMock()
    e.is_loaded = True
    e.model_version = "bge-m3_v1"
    e.aembed = AsyncMock(return_value=[FAKE_VECTOR])
    return e


def _make_vs():
    return {
        "upsert": AsyncMock(),
        "update_payload": AsyncMock(),
        "delete": AsyncMock(),
    }


def _make_settings() -> MagicMock:
    return MagicMock()


@contextmanager
def _client(prompts_dir: Path):
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
    app.state.embedder = _make_embedder()
    app.state.qdrant = MagicMock()
    app.state.scheduler = MagicMock()
    app.state.settings = _make_settings()

    vs = _make_vs()
    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
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
            yield http


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text("System {language}", encoding="utf-8")
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# POST /intents
# ---------------------------------------------------------------------------


def test_post_intent_no_templates_uses_default(prompts_dir: Path) -> None:
    """POST without template fields defaults to 'default' and succeeds."""
    with _client(prompts_dir) as http:
        resp = http.post("/intents", json=VALID_BODY)
    assert resp.status_code == 201
    data = resp.json()
    assert data["system_template"] == "default"
    assert data["instruction_template"] == "default"


def test_post_intent_nonexistent_system_template_returns_422(prompts_dir: Path) -> None:
    """POST with a non-existent system_template returns 422."""
    with _client(prompts_dir) as http:
        body = {**VALID_BODY, "system_template": "ghost"}
        resp = http.post("/intents", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "system_template"
    assert detail["value"] == "ghost"
    assert "not found" in detail["reason"]


def test_post_intent_nonexistent_instruction_template_returns_422(prompts_dir: Path) -> None:
    """POST with a non-existent instruction_template returns 422."""
    with _client(prompts_dir) as http:
        body = {**VALID_BODY, "instruction_template": "ghost"}
        resp = http.post("/intents", json=body)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "instruction_template"


def test_post_intent_path_traversal_returns_422(prompts_dir: Path) -> None:
    """POST with a path-traversal template name fails at the Pydantic syntax layer (422)."""
    with _client(prompts_dir) as http:
        body = {**VALID_BODY, "system_template": "../../etc/passwd"}
        resp = http.post("/intents", json=body)
    assert resp.status_code == 422


def test_post_intent_named_template_exists_succeeds(prompts_dir: Path) -> None:
    """POST with existing named template succeeds."""
    (prompts_dir / "instruction" / "brief.md").write_text(
        "Brief: {intent_text}\n{articles}", encoding="utf-8"
    )
    with _client(prompts_dir) as http:
        body = {**VALID_BODY, "instruction_template": "brief"}
        resp = http.post("/intents", json=body)
    assert resp.status_code == 201
    assert resp.json()["instruction_template"] == "brief"


# ---------------------------------------------------------------------------
# PUT /intents
# ---------------------------------------------------------------------------


def test_put_intent_change_to_nonexistent_template_returns_422(prompts_dir: Path) -> None:
    """PUT that changes instruction_template to a non-existent name returns 422."""
    with _client(prompts_dir) as http:
        create = http.post("/intents", json=VALID_BODY)
        assert create.status_code == 201
        intent_id = create.json()["id"]
        resp = http.put(f"/intents/{intent_id}", json={"instruction_template": "ghost"})
    assert resp.status_code == 422


def test_put_intent_no_template_change_skips_validation(prompts_dir: Path) -> None:
    """PUT that doesn't touch templates doesn't trigger template validation."""
    with _client(prompts_dir) as http:
        create = http.post("/intents", json=VALID_BODY)
        intent_id = create.json()["id"]
        resp = http.put(f"/intents/{intent_id}", json={"name": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "renamed"

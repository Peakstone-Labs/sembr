"""Tests for GET /api/prompts/templates routes."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.prompts import router


def _make_settings(prompts_dir: Path) -> MagicMock:
    s = MagicMock()
    s.prompts_dir = prompts_dir
    return s


@contextmanager
def _client(prompts_dir: Path):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.state.settings = _make_settings(prompts_dir)
    with TestClient(app) as http:
        yield http


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text(
        "System prompt {language}", encoding="utf-8"
    )
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# GET /api/prompts/templates
# ---------------------------------------------------------------------------


def test_list_templates_returns_both_kinds(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "system" in data
    assert "instruction" in data
    assert "default" in data["system"]
    assert "default" in data["instruction"]


def test_list_templates_sorted(prompts_dir: Path) -> None:
    (prompts_dir / "system" / "beta.md").write_text("x", encoding="utf-8")
    (prompts_dir / "system" / "alpha.md").write_text("x", encoding="utf-8")
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates")
    system_list = resp.json()["system"]
    assert system_list == sorted(system_list)


# ---------------------------------------------------------------------------
# GET /api/prompts/templates/{kind}/{name}
# ---------------------------------------------------------------------------


def test_get_template_returns_content(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates/system/default")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "default"
    assert data["kind"] == "system"
    assert "{language}" in data["content"]
    assert data["size_bytes"] > 0
    assert data["mtime"] > 0


def test_get_template_missing_returns_404(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates/system/ghost")
    assert resp.status_code == 404


def test_get_template_invalid_kind_returns_400(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates/invalid_kind/default")
    assert resp.status_code == 400


def test_get_template_path_traversal_returns_400(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates/system/..%2Fetc%2Fpasswd")
    assert resp.status_code in (400, 404)  # either validation or path rejection

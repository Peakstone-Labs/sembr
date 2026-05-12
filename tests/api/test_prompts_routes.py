"""Tests for GET /api/prompts/templates routes (post-rewrite shape)."""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.prompts import router
from sembr.dashboard.auth import DashboardTokenMiddleware
from sembr.db.intents import init_intent_tables
from sembr.db.sqlite import install_for_test


@contextmanager
def _client(prompts_dir: Path):
    """TestClient with in-memory SQLite (intents table empty by default).

    The new GET /templates handler reads `list_template_refs` from the shared
    aiosqlite connection on every call, so the test fixture provisions one
    via `install_for_test` inside the FastAPI lifespan (matches the pattern
    in `tests/test_intents.py`).
    """
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.state.settings = MagicMock()

    with patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir):
        with TestClient(app) as http:
            yield http


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text("System prompt {language}", encoding="utf-8")
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )
    return tmp_path


# ---------------------------------------------------------------------------
# GET /api/prompts/templates — rich shape per D6
# ---------------------------------------------------------------------------


def test_list_templates_returns_both_kinds(prompts_dir: Path) -> None:
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert "system" in data
    assert "instruction" in data
    sys_names = [r["name"] for r in data["system"]]
    inst_names = [r["name"] for r in data["instruction"]]
    assert "default" in sys_names
    assert "default" in inst_names


def test_list_templates_sorted(prompts_dir: Path) -> None:
    (prompts_dir / "system" / "beta.md").write_text("x", encoding="utf-8")
    (prompts_dir / "system" / "alpha.md").write_text("x", encoding="utf-8")
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates")
    sys_names = [r["name"] for r in resp.json()["system"]]
    assert sys_names == sorted(sys_names)


def test_list_templates_default_is_builtin_zero_refs(prompts_dir: Path) -> None:
    """`default` rows must have is_builtin=true and ref_count=0 on a fresh DB."""
    with _client(prompts_dir) as http:
        resp = http.get("/api/prompts/templates")
    data = resp.json()
    sys_default = next(r for r in data["system"] if r["name"] == "default")
    assert sys_default["is_builtin"] is True
    assert sys_default["ref_count"] == 0
    assert sys_default["ref_intents"] == []
    assert sys_default["size_bytes"] > 0
    assert sys_default["mtime"] > 0


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
    assert data["is_builtin"] is True
    assert data["ref_intents"] == []


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


# ---------------------------------------------------------------------------
# Auth gate — /api/prompts/* must be protected by DashboardTokenMiddleware
# ---------------------------------------------------------------------------


def test_prompts_routes_require_token_when_dashboard_token_set(prompts_dir: Path) -> None:
    """When DASHBOARD_TOKEN is configured, unauthenticated requests return 401."""
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    app.state.settings = MagicMock()

    mock_settings = MagicMock()
    mock_settings.dashboard_token.get_secret_value.return_value = "secret-token"

    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
        patch("sembr.dashboard.auth.get_settings", return_value=mock_settings),
    ):
        with TestClient(app, raise_server_exceptions=False) as http:
            resp = http.get("/api/prompts/templates")
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/api/prompts/templates/instruction", {"name": "x"}),
        (
            "PUT",
            "/api/prompts/templates/instruction/default",
            {"content": "Topic: {intent_text}\n{articles}"},
        ),
        ("DELETE", "/api/prompts/templates/instruction/default", None),
        ("POST", "/api/prompts/templates/instruction/default/rename", {"new_name": "x"}),
    ],
)
def test_prompts_write_verbs_require_token_when_dashboard_token_set(
    prompts_dir: Path, method: str, path: str, body
) -> None:
    """Each write verb is auth-gated by DashboardTokenMiddleware (per-method 401 e2e)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    app.state.settings = MagicMock()

    mock_settings = MagicMock()
    mock_settings.dashboard_token.get_secret_value.return_value = "secret-token"

    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
        patch("sembr.dashboard.auth.get_settings", return_value=mock_settings),
    ):
        with TestClient(app, raise_server_exceptions=False) as http:
            resp = http.request(method, path, json=body)
    assert resp.status_code == 401


def test_prompts_routes_allow_request_with_valid_token(prompts_dir: Path) -> None:
    """Requests with a valid X-Dashboard-Token header pass through."""
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import aiosqlite  # noqa: PLC0415

        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    app.state.settings = MagicMock()

    mock_settings = MagicMock()
    mock_settings.dashboard_token.get_secret_value.return_value = "secret-token"

    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
        patch("sembr.dashboard.auth.get_settings", return_value=mock_settings),
    ):
        with TestClient(app, raise_server_exceptions=False) as http:
            resp = http.get(
                "/api/prompts/templates",
                headers={"X-Dashboard-Token": "secret-token"},
            )
    assert resp.status_code == 200

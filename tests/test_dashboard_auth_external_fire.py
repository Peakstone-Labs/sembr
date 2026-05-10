"""Auth-middleware integration for the new ``/api/external/`` prefix.

Per design test-strategy: this file ONLY covers whether the prefix is gated by
``DashboardTokenMiddleware`` — endpoint behaviour (404/409/429/500) lives in
``test_api_external_fire.py``.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.config import get_settings
from sembr.dashboard.auth import DashboardTokenMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)

    @app.post("/api/external/intents/{intent_id}/fire")
    async def fake_fire(intent_id: int):
        return {"intent_id": intent_id, "ok": True}

    return app


def _set_token(monkeypatch, value: str) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DASHBOARD_TOKEN", value)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Each test must observe its own DASHBOARD_TOKEN value, regardless of
    whether the previous test had one."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_external_fire_route_protected_by_middleware(monkeypatch) -> None:
    """No header + token configured → 401. Confirms /api/external/ is in
    the _PROTECTED_PREFIXES tuple."""
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.post("/api/external/intents/1/fire", json={})
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_external_fire_passes_with_correct_token_header(monkeypatch) -> None:
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.post(
        "/api/external/intents/1/fire",
        json={},
        headers={"X-Dashboard-Token": "secret123"},
    )
    assert r.status_code == 200
    assert r.json()["intent_id"] == 1


def test_external_fire_rejects_wrong_token(monkeypatch) -> None:
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.post(
        "/api/external/intents/1/fire",
        json={},
        headers={"X-Dashboard-Token": "WRONG"},
    )
    assert r.status_code == 401


def test_external_fire_passes_through_when_token_unset(monkeypatch) -> None:
    """Empty DASHBOARD_TOKEN means pass-through (existing middleware contract);
    verify the new /api/external/ prefix inherits this behaviour."""
    _set_token(monkeypatch, "")
    client = TestClient(_make_app())
    r = client.post("/api/external/intents/1/fire", json={})
    assert r.status_code == 200

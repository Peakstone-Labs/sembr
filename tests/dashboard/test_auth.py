# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr.dashboard.auth.DashboardTokenMiddleware.

Cover:
  (a) no token configured → /api/dashboard/* passes through
  (b) token configured + missing header → 401 on /api/, 302 on /dashboard/*
  (c) token configured + wrong header → 401
  (d) token configured + correct header → 200
  (e) token configured + correct cookie → 200
  (f) /api/feeds is unaffected by middleware (no false-positive blocks)
  (g) /dashboard/login.html is always reachable
  (h) /api/dashboard/config is always reachable (frontend bootstrap)
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.config import get_settings
from sembr.dashboard.auth import DashboardTokenMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)

    @app.get("/api/dashboard/snapshot")
    async def snap():
        return {"ok": True}

    @app.get("/api/dashboard/config")
    async def cfg():
        return {"poll_interval_seconds": 10, "auth_required": True}

    @app.get("/dashboard/index.html")
    async def index():
        return {"page": "index"}

    @app.get("/dashboard/login.html")
    async def login():
        return {"page": "login"}

    @app.get("/api/feeds")
    async def feeds():
        return {"feeds": []}

    return app


def _set_token(monkeypatch, value: str) -> None:
    """Bypass pydantic-settings env caching by overriding get_settings cache."""
    get_settings.cache_clear()
    monkeypatch.setenv("DASHBOARD_TOKEN", value)


def test_no_token_passes_through(monkeypatch):
    _set_token(monkeypatch, "")
    client = TestClient(_make_app())
    assert client.get("/api/dashboard/snapshot").status_code == 200


def test_missing_token_returns_401_on_api(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get("/api/dashboard/snapshot")
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}


def test_missing_token_redirects_html(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get("/dashboard/index.html", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard/login.html"


def test_wrong_token_returns_401(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get(
        "/api/dashboard/snapshot",
        headers={"X-Dashboard-Token": "nope"},
    )
    assert r.status_code == 401


def test_correct_header_token_grants_access(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get(
        "/api/dashboard/snapshot",
        headers={"X-Dashboard-Token": "secret123"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_correct_cookie_token_grants_access(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    client.cookies.set("sembr_dashboard_token", "secret123")
    r = client.get("/dashboard/index.html")
    assert r.status_code == 200


def test_business_api_unaffected(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get("/api/feeds")
    assert r.status_code == 200


def test_login_page_always_reachable(monkeypatch):
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get("/dashboard/login.html")
    assert r.status_code == 200
    assert r.json() == {"page": "login"}


def test_config_endpoint_is_auth_free(monkeypatch):
    """Frontend must read /api/dashboard/config before having a token to know
    if auth is even required — if this endpoint is gated, the login page can't load."""
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get("/api/dashboard/config")
    assert r.status_code == 200


def test_path_prefix_does_not_capture_dashboard_lookalike(monkeypatch):
    """Routes like /dashboard-status or /api/dashboard-stats must NOT fall under
    the gate just because they share the /dashboard prefix as a substring."""
    _set_token(monkeypatch, "secret123")
    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)

    @app.get("/dashboard-status")
    async def dash_status():
        return {"unrelated": True}

    @app.get("/api/dashboard-stats")
    async def dash_stats():
        return {"unrelated": True}

    client = TestClient(app)
    assert client.get("/dashboard-status").status_code == 200
    assert client.get("/api/dashboard-stats").status_code == 200


def test_token_not_subject_to_timing_attack_via_length(monkeypatch):
    """compare_digest accepts any-length string and returns False; a length-mismatch
    must NOT be ValueError'd by the middleware (would 500 instead of 401)."""
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app())
    r = client.get(
        "/api/dashboard/snapshot",
        headers={"X-Dashboard-Token": "x"},
    )
    assert r.status_code == 401

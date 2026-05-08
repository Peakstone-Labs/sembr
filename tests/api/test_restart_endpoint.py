"""Unit tests for sembr.api.restart — POST /api/dashboard/restart (design D1).

Auth model and response shape mirror /api/settings/save:
- Header-only auth (Depends(require_header_token)) — defends against
  cookie-based CSRF
- 200 + rsshub_restart_failed flag when rsshub recreate fails (api self-restart
  still proceeds)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api import restart as restart_router_mod
from sembr.api.restart import router
from sembr.api.settings_restart import RestartController
from sembr.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def fake_rc() -> MagicMock:
    rc = MagicMock(spec=RestartController)

    async def _restart_rsshub(*a, **k):
        return None

    rc.restart_rsshub.side_effect = _restart_rsshub
    rc.schedule_self_restart.return_value = None
    return rc


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    return a


def test_restart_calls_rsshub_then_self_restart(
    app: FastAPI, fake_rc: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """Happy path: 200 + both restart actions invoked, no failure flag."""
    monkeypatch.setattr(restart_router_mod, "RestartController", lambda *a, **kw: fake_rc)
    client = TestClient(app)

    r = client.post("/api/dashboard/restart")
    assert r.status_code == 200
    body = r.json()
    assert body == {"rsshub_restart_failed": False, "rsshub_error": None}

    # Order matters (settings.py:481-501): rsshub first (await), then schedule
    # api SIGTERM — reversing would let SIGTERM fire mid-response.
    fake_rc.restart_rsshub.assert_called_once()
    fake_rc.schedule_self_restart.assert_called_once()


def test_restart_rsshub_failure_returns_200_with_flag(
    app: FastAPI, fake_rc: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """Per D1: rsshub failure becomes 200 + flag (not 500). The api self-restart
    must still proceed so disk + process state converge regardless of rsshub."""
    async def _fail(*a, **k):
        raise RuntimeError("compose recreate timed out")

    fake_rc.restart_rsshub.side_effect = _fail
    monkeypatch.setattr(restart_router_mod, "RestartController", lambda *a, **kw: fake_rc)
    client = TestClient(app)

    r = client.post("/api/dashboard/restart")
    assert r.status_code == 200
    body = r.json()
    assert body["rsshub_restart_failed"] is True
    assert "compose recreate timed out" in body["rsshub_error"]
    fake_rc.schedule_self_restart.assert_called_once()


def test_restart_requires_header_token_when_token_set(
    app: FastAPI, fake_rc: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    """When DASHBOARD_TOKEN is configured, the endpoint must reject requests
    that don't carry the X-Dashboard-Token header — mirrors the CSRF defence
    on /api/settings/save (settings.py:85-103)."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-xyz")
    monkeypatch.setattr(restart_router_mod, "RestartController", lambda *a, **kw: fake_rc)
    get_settings.cache_clear()
    client = TestClient(app)

    # Without header → 401
    r = client.post("/api/dashboard/restart")
    assert r.status_code == 401
    fake_rc.restart_rsshub.assert_not_called()
    fake_rc.schedule_self_restart.assert_not_called()

    # With matching header → 200
    r = client.post(
        "/api/dashboard/restart",
        headers={"X-Dashboard-Token": "secret-xyz"},
    )
    assert r.status_code == 200
    fake_rc.restart_rsshub.assert_called_once()
    fake_rc.schedule_self_restart.assert_called_once()

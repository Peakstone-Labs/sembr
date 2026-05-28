# SPDX-License-Identifier: Apache-2.0
"""Auth-middleware coverage for the three new history endpoints.

Confirms ``/intents/`` prefix inherited protection from
``DashboardTokenMiddleware._PROTECTED_PREFIXES``.  Non-API paths get a 302
redirect to the login page (not 401) — pass ``follow_redirects=False`` so we
assert the gate itself, not the (missing) login page.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.config import get_settings
from sembr.dashboard.auth import DashboardTokenMiddleware


def _make_app() -> FastAPI:
    from sembr.api.history import router

    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    return app


def _set_token(monkeypatch, value: str) -> None:
    get_settings.cache_clear()
    monkeypatch.setenv("DASHBOARD_TOKEN", value)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_aggregate_unauthenticated(monkeypatch) -> None:
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.post(
        "/intents/1/history/aggregate",
        json={"since": "2026-05-01", "until": "2026-05-28", "prompt": "test {history}"},
    )
    assert r.status_code == 302


def test_aggregate_send_unauthenticated(monkeypatch) -> None:
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.post(
        "/intents/1/history/aggregate/send",
        json={"since": "2026-05-01", "until": "2026-05-28", "markdown": "test"},
    )
    assert r.status_code == 302


def test_export_unauthenticated(monkeypatch) -> None:
    _set_token(monkeypatch, "secret123")
    client = TestClient(_make_app(), follow_redirects=False)
    r = client.get("/intents/1/history/export?since=2026-05-01&until=2026-05-28")
    assert r.status_code == 302

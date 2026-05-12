"""Integration tests: settings save + self-restart lifecycle.

Verifies that:
1. TestClient never triggers _force_exit — the conditional is exclusively
   in the lifespan finally block, not in request handling.
2. schedule_self_restart correctly wires up call_later + sets _RESTART_REQUESTED
   when the callback fires.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api import settings as settings_router_mod
from sembr.api import settings_restart
from sembr.api.settings import router
from sembr.api.settings_restart import RestartController
from sembr.config import get_settings


SAMPLE_ENV = """\
QDRANT_URL=http://qdrant:6333
EMBEDDER_API_KEY=sk-test
EMBEDDER_MODEL=BAAI/bge-m3
DASHBOARD_TOKEN=
"""


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def reset_restart_flag():
    settings_restart._RESTART_REQUESTED = False
    yield
    settings_restart._RESTART_REQUESTED = False


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / ".env"
    p.write_text(SAMPLE_ENV, encoding="utf-8")
    monkeypatch.setattr(settings_router_mod, "ENV_FILE_PATH", p)
    return p


@pytest.fixture
def fake_rc() -> MagicMock:
    rc = MagicMock(spec=RestartController)

    async def _noop(*a, **k):
        return None

    rc.restart_rsshub.side_effect = _noop
    rc.schedule_self_restart.return_value = None
    return rc


@pytest.fixture
def app(env_file: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_save_settings_no_restart_in_test_client(
    app: FastAPI,
    fake_rc: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TestClient hitting /api/settings/save must never call _force_exit.

    _force_exit lives exclusively in the lifespan finally block; there is no
    path from request handling to _force_exit.  This test guards against a
    future regression where someone moves the call into the request path.
    """
    force_exit_calls: list[int] = []
    monkeypatch.setattr(settings_restart, "_force_exit", lambda code: force_exit_calls.append(code))
    monkeypatch.setattr(settings_router_mod, "RestartController", lambda *a, **kw: fake_rc)

    client = TestClient(app)
    r = client.post(
        "/api/settings/save",
        json={"changes": {"QDRANT_URL": "http://other:6333"}, "confirmed": True},
    )
    assert r.status_code == 200
    # Verify the request actually reached the restart path (not a vacuous test)
    fake_rc.schedule_self_restart.assert_called_once()
    assert force_exit_calls == [], "_force_exit must never be called during request handling"


@pytest.mark.asyncio
async def test_save_settings_schedules_self_restart_call_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """schedule_self_restart() registers call_later; when it fires, _RESTART_REQUESTED
    becomes True (simulated via a _spawn_self_force_recreate that sets the flag but
    suppresses the actual fork)."""
    fired: list[bool] = []

    def fake_spawn():
        settings_restart._RESTART_REQUESTED = True
        fired.append(True)

    monkeypatch.setattr(settings_restart, "_spawn_self_force_recreate", fake_spawn)

    loop = asyncio.get_running_loop()
    rc = RestartController(loop=loop, subprocess_runner=lambda *a, **k: None)
    rc.schedule_self_restart(delay=0.05)

    assert not settings_restart.is_restart_requested(), "flag must not be set before callback fires"

    await asyncio.sleep(0.15)

    assert fired == [True], "call_later callback must have fired"
    assert settings_restart.is_restart_requested(), "_RESTART_REQUESTED must be True after callback"

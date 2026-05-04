"""Tests for sembr.api.settings router (FastAPI TestClient + monkeypatched envfile)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api import settings as settings_router_mod
from sembr.api.settings import SENSITIVE_MASK, router
from sembr.api.settings_envfile import USER_ADDITIONS_HEADER
from sembr.api.settings_restart import RestartController
from sembr.config import Settings, get_settings


SAMPLE_ENV = """\
API_HOST=0.0.0.0
API_PORT=8000
EMBEDDER_API_KEY=sk-original
EMBEDDER_MODEL=BAAI/bge-m3
SMTP_PASSWORD=plaintext
DASHBOARD_TOKEN=
TWITTER_COOKIE=ct0=abc
SOMETHING_LEGACY=value
"""


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / ".env"
    p.write_text(SAMPLE_ENV, encoding="utf-8")
    monkeypatch.setattr(settings_router_mod, "ENV_FILE_PATH", p)
    return p


@pytest.fixture
def app(env_file: Path) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def fake_rc() -> MagicMock:
    rc = MagicMock(spec=RestartController)

    async def _restart_rsshub(*a, **k):
        return None

    rc.restart_rsshub.side_effect = _restart_rsshub
    return rc


@pytest.fixture
def client(app: FastAPI, fake_rc: MagicMock, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings_router_mod, "RestartController", lambda *a, **kw: fake_rc)
    return TestClient(app)


# ── /schema ───────────────────────────────────────────────────────────────


def test_schema_returns_sembr_fields_and_passthrough(client: TestClient) -> None:
    r = client.get("/api/settings/schema")
    assert r.status_code == 200
    body = r.json()
    keys = {f["key"] for f in body["sembr_fields"]}
    assert "API_HOST" in keys
    assert "EMBEDDER_API_KEY" in keys
    assert "DASHBOARD_TOKEN" in keys
    # Sensitive marking
    secret_field = next(f for f in body["sembr_fields"] if f["key"] == "EMBEDDER_API_KEY")
    assert secret_field["sensitive"] is True
    assert secret_field["type"] == "secret"
    # Enum field
    enum_field = next(f for f in body["sembr_fields"] if f["key"] == "EMBEDDER_BACKEND")
    assert enum_field["type"] == "enum"
    assert enum_field["enum"] == ["siliconflow"]
    # Numeric ge/le
    threshold = next(f for f in body["sembr_fields"] if f["key"] == "LLM_GROUPING_THRESHOLD")
    assert threshold["ge"] == 0.0 and threshold["le"] == 1.0
    # Passthrough prefixes
    assert "TWITTER_" in body["passthrough_prefixes"]
    assert "GITHUB_" in body["passthrough_prefixes"]


# ── /values ───────────────────────────────────────────────────────────────


def test_values_masks_sensitive_fields(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["API_HOST"] == "0.0.0.0"
    assert body["values"]["EMBEDDER_API_KEY"] == SENSITIVE_MASK
    assert body["values"]["SMTP_PASSWORD"] == SENSITIVE_MASK
    # Passthrough secret-named field also masked
    assert body["values"]["TWITTER_COOKIE"] == SENSITIVE_MASK


def test_values_unknown_keys_separated(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    body = r.json()
    keys = {u["key"] for u in body["unknown_keys"]}
    assert "SOMETHING_LEGACY" in keys


def test_values_overridden_by_shell_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_HOST", "shellvalue")
    r = client.get("/api/settings/values")
    body = r.json()
    assert "API_HOST" in body["overridden_by_shell_env"]


def test_values_env_file_injection_not_flagged_as_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compose env_file: injects every .env key into os.environ. Same-value
    presence must NOT flag the field as shell-overridden — only true value
    mismatches qualify."""
    # Match the fixture .env value byte-for-byte.
    monkeypatch.setenv("API_HOST", "0.0.0.0")
    monkeypatch.setenv("EMBEDDER_API_KEY", "sk-original")
    r = client.get("/api/settings/values")
    body = r.json()
    assert "API_HOST" not in body["overridden_by_shell_env"]
    assert "EMBEDDER_API_KEY" not in body["overridden_by_shell_env"]


def test_values_partial_override_only_flags_changed_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed scenario: same-value injection on one key, true override on another."""
    monkeypatch.setenv("API_HOST", "0.0.0.0")     # matches .env → not overridden
    monkeypatch.setenv("API_PORT", "9999")         # differs from .env=8000 → overridden
    r = client.get("/api/settings/values")
    body = r.json()
    assert "API_HOST" not in body["overridden_by_shell_env"]
    assert "API_PORT" in body["overridden_by_shell_env"]


def test_values_empty_secret_not_masked(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    body = r.json()
    # DASHBOARD_TOKEN is empty in fixture → return "" not the mask
    assert body["values"]["DASHBOARD_TOKEN"] == ""


# ── /save ─────────────────────────────────────────────────────────────────


def test_save_requires_confirmed_true(client: TestClient) -> None:
    r = client.post("/api/settings/save", json={"changes": {"API_HOST": "1.2.3.4"}, "confirmed": False})
    assert r.status_code == 422


def test_save_missing_confirmed_field(client: TestClient) -> None:
    r = client.post("/api/settings/save", json={"changes": {"API_HOST": "1.2.3.4"}})
    assert r.status_code == 422


def test_save_sembr_field_changes_only_targets_api(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    r = client.post(
        "/api/settings/save",
        json={"changes": {"API_HOST": "127.0.0.1"}, "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["restart_targets"] == ["api"]
    assert body["saved_keys"] == ["API_HOST"]
    text = env_file.read_text(encoding="utf-8")
    assert "API_HOST=127.0.0.1" in text
    fake_rc.schedule_self_restart.assert_called_once()
    fake_rc.restart_rsshub.assert_not_called()


def test_save_passthrough_addition_targets_both(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    r = client.post(
        "/api/settings/save",
        json={"additions": {"TELEGRAM_TOKEN": "abc:def"}, "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert set(body["restart_targets"]) == {"api", "rsshub"}
    assert body["rsshub_restart_failed"] is False
    text = env_file.read_text(encoding="utf-8")
    assert "TELEGRAM_TOKEN=abc:def" in text
    assert USER_ADDITIONS_HEADER in text
    fake_rc.restart_rsshub.assert_called_once()
    fake_rc.schedule_self_restart.assert_called_once()


# 🔴-1: addition with mask sentinel must be rejected as 422.
def test_save_addition_with_mask_sentinel_rejected(
    client: TestClient, env_file: Path
) -> None:
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"additions": {"TWITTER_COOKIE": SENSITIVE_MASK}, "confirmed": True},
    )
    assert r.status_code == 422
    assert "mask sentinel" in r.json()["detail"].lower()
    assert env_file.read_text(encoding="utf-8") == original


# 🟡-1: rsshub restart failure must NOT block the response and api self-restart
# must still be scheduled. Status remains 200 with rsshub_restart_failed=true.
def test_save_rsshub_failure_downgrades_to_warning(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    async def boom(*a, **k):
        raise RuntimeError("docker daemon unreachable")
    fake_rc.restart_rsshub.side_effect = boom

    r = client.post(
        "/api/settings/save",
        json={"additions": {"TWITTER_COOKIE": "ct0=xyz"}, "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rsshub_restart_failed"] is True
    assert "docker daemon unreachable" in body["rsshub_error"]
    assert set(body["restart_targets"]) == {"api", "rsshub"}
    # api self-restart was scheduled despite the rsshub failure
    fake_rc.schedule_self_restart.assert_called_once()
    # Disk write happened
    assert "TWITTER_COOKIE=ct0=xyz" in env_file.read_text(encoding="utf-8")


# Loop 2 🟡-A: schedule_self_restart must run AFTER restart_rsshub awaits.
# Scheduling earlier would let SIGTERM (1.5s) fire mid-await and drop the response.
def test_save_passthrough_schedules_self_restart_after_rsshub(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    r = client.post(
        "/api/settings/save",
        json={"additions": {"GITHUB_ACCESS_TOKEN": "ghp_xxx"}, "confirmed": True},
    )
    assert r.status_code == 200
    call_order = [c[0] for c in fake_rc.method_calls]
    rsshub_idx = call_order.index("restart_rsshub")
    schedule_idx = call_order.index("schedule_self_restart")
    assert rsshub_idx < schedule_idx, (
        f"schedule_self_restart must come after restart_rsshub, got order: {call_order}"
    )


def test_save_rejects_non_whitelist_key(
    client: TestClient, env_file: Path
) -> None:
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"additions": {"MY_RANDOM_VAR": "foo"}, "confirmed": True},
    )
    assert r.status_code == 422
    body = r.json()
    detail = body["detail"]
    assert "passthrough" in detail["error"]
    assert "MY_RANDOM_VAR" in detail["rejected_keys"]
    assert "TWITTER_" in detail["allowed_prefixes"]
    # Disk untouched
    assert env_file.read_text(encoding="utf-8") == original


def test_save_mask_sentinel_does_not_overwrite_secret(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    # Read masked values, then submit them back along with a non-sensitive
    # change. The sensitive byte sequence on disk must be byte-identical.
    original_text = env_file.read_text(encoding="utf-8")
    assert "EMBEDDER_API_KEY=sk-original" in original_text

    r = client.post(
        "/api/settings/save",
        json={
            "changes": {
                "API_HOST": "10.0.0.5",
                "EMBEDDER_API_KEY": SENSITIVE_MASK,
                "SMTP_PASSWORD": SENSITIVE_MASK,
            },
            "confirmed": True,
        },
    )
    assert r.status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert "EMBEDDER_API_KEY=sk-original" in text
    assert "SMTP_PASSWORD=plaintext" in text
    assert "API_HOST=10.0.0.5" in text
    body = r.json()
    # Mask-only submissions don't appear in saved_keys (no-op skipped).
    assert "EMBEDDER_API_KEY" not in body["saved_keys"]
    assert "SMTP_PASSWORD" not in body["saved_keys"]
    assert "API_HOST" in body["saved_keys"]


def test_save_real_secret_value_overwrites(
    client: TestClient, env_file: Path
) -> None:
    r = client.post(
        "/api/settings/save",
        json={"changes": {"EMBEDDER_API_KEY": "sk-newvalue"}, "confirmed": True},
    )
    assert r.status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert "EMBEDDER_API_KEY=sk-newvalue" in text
    assert "sk-original" not in text


def test_save_deletions(client: TestClient, env_file: Path) -> None:
    r = client.post(
        "/api/settings/save",
        json={"deletions": ["TWITTER_COOKIE"], "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert "TWITTER_COOKIE" in body["deleted_keys"]
    text = env_file.read_text(encoding="utf-8")
    assert "TWITTER_COOKIE" not in text


def test_save_no_op_returns_empty_targets(
    client: TestClient, fake_rc: MagicMock
) -> None:
    r = client.post("/api/settings/save", json={"confirmed": True})
    assert r.status_code == 200
    body = r.json()
    assert body["restart_targets"] == []
    fake_rc.schedule_self_restart.assert_not_called()
    fake_rc.restart_rsshub.assert_not_called()


def test_save_creates_bak(client: TestClient, env_file: Path) -> None:
    r = client.post(
        "/api/settings/save",
        json={"changes": {"API_HOST": "5.5.5.5"}, "confirmed": True},
    )
    assert r.status_code == 200
    bak = env_file.with_name(".env.bak")
    assert bak.exists()
    assert "API_HOST=0.0.0.0" in bak.read_text(encoding="utf-8")


# ── auth ──────────────────────────────────────────────────────────────────


def test_auth_required_when_token_set(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app)
    r = c.get("/api/settings/schema")
    assert r.status_code == 401

    r2 = c.get("/api/settings/schema", headers={"X-Dashboard-Token": "supersecret"})
    assert r2.status_code == 200


def test_auth_rejects_cookie_only(
    monkeypatch: pytest.MonkeyPatch, app: FastAPI
) -> None:
    """Cookie alone must not authenticate /api/settings/* (CSRF protection)."""
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app)
    c.cookies.set("sembr_dashboard_token", "supersecret")
    r = c.get("/api/settings/schema")
    assert r.status_code == 401


def test_auth_passthrough_when_no_token_configured(client: TestClient) -> None:
    """Empty DASHBOARD_TOKEN → router behaves as public (matches middleware)."""
    r = client.get("/api/settings/schema")
    assert r.status_code == 200


# 🟡-3: with the real DashboardTokenMiddleware mounted, cookie-only requests
# pass middleware but must still be rejected by the router's header dependency.
# This is the actual CSRF threat model for Decision #15.

@pytest.fixture
def app_with_middleware(env_file: Path) -> FastAPI:
    from sembr.dashboard.auth import DashboardTokenMiddleware

    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    return app


def test_cookie_passes_middleware_but_rejected_by_router_dep(
    monkeypatch: pytest.MonkeyPatch, app_with_middleware: FastAPI
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app_with_middleware)
    c.cookies.set("sembr_dashboard_token", "supersecret")
    r = c.get("/api/settings/schema")
    # Middleware sees a valid cookie → passes through.
    # Router dependency requires X-Dashboard-Token header → 401.
    assert r.status_code == 401


def test_header_passes_both_middleware_and_router(
    monkeypatch: pytest.MonkeyPatch, app_with_middleware: FastAPI
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app_with_middleware)
    r = c.get("/api/settings/schema", headers={"X-Dashboard-Token": "supersecret"})
    assert r.status_code == 200


def test_no_token_rejected_by_middleware(
    monkeypatch: pytest.MonkeyPatch, app_with_middleware: FastAPI
) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app_with_middleware)
    r = c.get("/api/settings/schema")
    assert r.status_code == 401

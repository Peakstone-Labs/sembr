# SPDX-License-Identifier: Apache-2.0
"""Tests for sembr.api.settings router (FastAPI TestClient + monkeypatched envfile)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api import settings as settings_router_mod
from sembr.api.settings import SENSITIVE_MASK, router
from sembr.api.settings_envfile import USER_ADDITIONS_HEADER
from sembr.api.settings_restart import RestartController
from sembr.config import get_settings

SAMPLE_ENV = """\
QDRANT_URL=http://qdrant:6333
DASHBOARD_LOG_RETENTION_DAYS=7
EMBEDDER_API_KEY=sk-original
EMBEDDER_MODEL=BAAI/bge-m3
SMTP_PASSWORD=plaintext
DASHBOARD_TOKEN=
TWITTER_AUTH_TOKEN=abcdef0123456789
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
    assert "QDRANT_URL" in keys
    assert "EMBEDDER_API_KEY" in keys
    assert "DASHBOARD_TOKEN" in keys
    # API_HOST / API_PORT removed (Dockerfile hardcodes); not in Settings anymore.
    assert "API_HOST" not in keys
    assert "API_PORT" not in keys
    # Hidden-from-UI: EMBEDDER_BACKEND (single-value Literal) is filtered out.
    assert "EMBEDDER_BACKEND" not in keys
    # Sensitive marking
    secret_field = next(f for f in body["sembr_fields"] if f["key"] == "EMBEDDER_API_KEY")
    assert secret_field["sensitive"] is True
    assert secret_field["type"] == "secret"
    # Numeric ge/le — pick a numeric field with a documented bound; the field set
    # evolves across loops, so this assertion only checks that ge/le metadata is
    # exposed correctly for at least one numeric field.
    budget = next(f for f in body["sembr_fields"] if f["key"] == "LLM_MAX_PROMPT_CHARS")
    assert budget["ge"] == 2_000
    # Passthrough prefixes + recommended
    assert "TWITTER_" in body["passthrough_prefixes"]
    assert "GITHUB_" in body["passthrough_prefixes"]
    rec_keys = {r["key"] for r in body["passthrough_recommended"]}
    assert {
        "TWITTER_AUTH_TOKEN",
        "TELEGRAM_TOKEN",
        "TELEGRAM_SESSION",
        "GITHUB_ACCESS_TOKEN",
    } <= rec_keys


# ── /values ───────────────────────────────────────────────────────────────


def test_values_masks_sensitive_fields(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["QDRANT_URL"] == "http://qdrant:6333"
    assert body["values"]["EMBEDDER_API_KEY"] == SENSITIVE_MASK
    assert body["values"]["SMTP_PASSWORD"] == SENSITIVE_MASK
    # Passthrough secret-named field also masked
    assert body["values"]["TWITTER_AUTH_TOKEN"] == SENSITIVE_MASK


def test_values_hidden_fields_not_returned(client: TestClient, env_file: Path) -> None:
    """Hidden-from-UI sembr fields must not appear in /values either —
    otherwise the frontend mis-classifies them as passthrough keys
    (since schema also excludes them, frontend sees no sembr-key match)."""
    # Add a hidden field to the .env fixture and reload.
    contents = env_file.read_text(encoding="utf-8")
    env_file.write_text(contents + "EMBEDDER_BACKEND=siliconflow\n", encoding="utf-8")

    r = client.get("/api/settings/values")
    body = r.json()
    assert "EMBEDDER_BACKEND" not in body["values"]
    # Also must not leak into unknown_keys (it's a known sembr field, just hidden).
    unknown_keys = {u["key"] for u in body["unknown_keys"]}
    assert "EMBEDDER_BACKEND" not in unknown_keys


def test_values_unknown_keys_separated(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    body = r.json()
    keys = {u["key"] for u in body["unknown_keys"]}
    assert "SOMETHING_LEGACY" in keys


def test_values_overridden_by_shell_env(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QDRANT_URL", "http://other:6333")
    r = client.get("/api/settings/values")
    body = r.json()
    assert "QDRANT_URL" in body["overridden_by_shell_env"]


def test_values_env_file_injection_not_flagged_as_override(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """compose env_file: injects every .env key into os.environ. Same-value
    presence must NOT flag the field as shell-overridden — only true value
    mismatches qualify."""
    # Match the fixture .env value byte-for-byte.
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    monkeypatch.setenv("EMBEDDER_API_KEY", "sk-original")
    r = client.get("/api/settings/values")
    body = r.json()
    assert "QDRANT_URL" not in body["overridden_by_shell_env"]
    assert "EMBEDDER_API_KEY" not in body["overridden_by_shell_env"]


def test_values_partial_override_only_flags_changed_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed scenario: same-value injection on one key, true override on another."""
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")  # matches .env → not overridden
    monkeypatch.setenv("DASHBOARD_LOG_RETENTION_DAYS", "30")  # differs from .env=7 → overridden
    r = client.get("/api/settings/values")
    body = r.json()
    assert "QDRANT_URL" not in body["overridden_by_shell_env"]
    assert "DASHBOARD_LOG_RETENTION_DAYS" in body["overridden_by_shell_env"]


def test_values_empty_secret_not_masked(client: TestClient) -> None:
    r = client.get("/api/settings/values")
    body = r.json()
    # DASHBOARD_TOKEN is empty in fixture → return "" not the mask
    assert body["values"]["DASHBOARD_TOKEN"] == ""


# ── /save ─────────────────────────────────────────────────────────────────


def test_save_requires_confirmed_true(client: TestClient) -> None:
    r = client.post(
        "/api/settings/save", json={"changes": {"QDRANT_URL": "http://q:6333"}, "confirmed": False}
    )
    assert r.status_code == 422


def test_save_missing_confirmed_field(client: TestClient) -> None:
    r = client.post("/api/settings/save", json={"changes": {"QDRANT_URL": "http://q:6333"}})
    assert r.status_code == 422


def test_save_sembr_field_changes_only_targets_api(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    r = client.post(
        "/api/settings/save",
        json={"changes": {"QDRANT_URL": "http://other:6333"}, "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["restart_targets"] == ["api"]
    assert body["saved_keys"] == ["QDRANT_URL"]
    text = env_file.read_text(encoding="utf-8")
    assert "QDRANT_URL=http://other:6333" in text
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
def test_save_addition_with_mask_sentinel_rejected(client: TestClient, env_file: Path) -> None:
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"additions": {"TWITTER_AUTH_TOKEN": SENSITIVE_MASK}, "confirmed": True},
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
        json={"additions": {"TWITTER_AUTH_TOKEN": "cafebabe0123"}, "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["rsshub_restart_failed"] is True
    assert "docker daemon unreachable" in body["rsshub_error"]
    assert set(body["restart_targets"]) == {"api", "rsshub"}
    # api self-restart was scheduled despite the rsshub failure
    fake_rc.schedule_self_restart.assert_called_once()
    # Disk write happened
    assert "TWITTER_AUTH_TOKEN=cafebabe0123" in env_file.read_text(encoding="utf-8")


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


def test_save_rejects_non_whitelist_key(client: TestClient, env_file: Path) -> None:
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


# Settings validator dry-run: invalid sembr-class values must 422 and NOT
# write .env. Otherwise the bad value would crash the next force-recreate's
# Settings() construction in lifespan startup, causing a restart loop.


def test_save_rejects_invalid_newsapi_categories(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    """Foo is not in NEWSAPI_VALID_CATEGORIES → ValidationError → 422."""
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"changes": {"NEWSAPI_CATEGORIES": "Foo"}, "confirmed": True},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "Settings validation failed" in detail["error"]
    assert "NEWSAPI_CATEGORIES" in detail["rejected_fields"]
    # Disk untouched, no restart triggered.
    assert env_file.read_text(encoding="utf-8") == original
    fake_rc.schedule_self_restart.assert_not_called()


def test_save_rejects_empty_newsapi_categories(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    """Empty CSV violates non-empty validator (whitelist contract)."""
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"changes": {"NEWSAPI_CATEGORIES": ""}, "confirmed": True},
    )
    assert r.status_code == 422
    assert env_file.read_text(encoding="utf-8") == original
    fake_rc.schedule_self_restart.assert_not_called()


def test_save_rejects_out_of_range_int(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    """NEWSAPI_POLL_INTERVAL_MINUTES has ge=5, le=1440. 0 or 9999 must 422."""
    original = env_file.read_text(encoding="utf-8")
    r = client.post(
        "/api/settings/save",
        json={"changes": {"NEWSAPI_POLL_INTERVAL_MINUTES": "0"}, "confirmed": True},
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "NEWSAPI_POLL_INTERVAL_MINUTES" in detail["rejected_fields"]
    assert env_file.read_text(encoding="utf-8") == original
    fake_rc.schedule_self_restart.assert_not_called()


def test_save_accepts_valid_newsapi_categories(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    """Sanity check the validator doesn't reject canonical values."""
    r = client.post(
        "/api/settings/save",
        json={
            "changes": {"NEWSAPI_CATEGORIES": "Business,Sports,Health"},
            "confirmed": True,
        },
    )
    assert r.status_code == 200
    text = env_file.read_text(encoding="utf-8")
    assert "NEWSAPI_CATEGORIES=Business,Sports,Health" in text
    fake_rc.schedule_self_restart.assert_called_once()


def test_save_validator_skipped_for_passthrough_only_changes(
    client: TestClient, env_file: Path, fake_rc: MagicMock
) -> None:
    """Pure passthrough additions don't trigger Settings() dry-run since
    they're not Settings fields. Avoids unrelated Settings() side effects
    when user only edits RSSHub passthrough vars."""
    r = client.post(
        "/api/settings/save",
        json={"additions": {"OPENAI_API_KEY": "sk-test"}, "confirmed": True},
    )
    assert r.status_code == 200
    assert "OPENAI_API_KEY=sk-test" in env_file.read_text(encoding="utf-8")


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
                "QDRANT_URL": "http://other:6333",
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
    assert "QDRANT_URL=http://other:6333" in text
    body = r.json()
    # Mask-only submissions don't appear in saved_keys (no-op skipped).
    assert "EMBEDDER_API_KEY" not in body["saved_keys"]
    assert "SMTP_PASSWORD" not in body["saved_keys"]
    assert "QDRANT_URL" in body["saved_keys"]


def test_save_real_secret_value_overwrites(client: TestClient, env_file: Path) -> None:
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
        json={"deletions": ["TWITTER_AUTH_TOKEN"], "confirmed": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert "TWITTER_AUTH_TOKEN" in body["deleted_keys"]
    text = env_file.read_text(encoding="utf-8")
    assert "TWITTER_AUTH_TOKEN" not in text


def test_save_no_op_returns_empty_targets(client: TestClient, fake_rc: MagicMock) -> None:
    r = client.post("/api/settings/save", json={"confirmed": True})
    assert r.status_code == 200
    body = r.json()
    assert body["restart_targets"] == []
    fake_rc.schedule_self_restart.assert_not_called()
    fake_rc.restart_rsshub.assert_not_called()


def test_save_creates_bak(client: TestClient, env_file: Path) -> None:
    r = client.post(
        "/api/settings/save",
        json={"changes": {"QDRANT_URL": "http://changed:6333"}, "confirmed": True},
    )
    assert r.status_code == 200
    bak = env_file.with_name(".env.bak")
    assert bak.exists()
    assert "QDRANT_URL=http://qdrant:6333" in bak.read_text(encoding="utf-8")


# ── auth ──────────────────────────────────────────────────────────────────


def test_auth_required_when_token_set(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
    monkeypatch.setenv("DASHBOARD_TOKEN", "supersecret")
    get_settings.cache_clear()

    c = TestClient(app)
    r = c.get("/api/settings/schema")
    assert r.status_code == 401

    r2 = c.get("/api/settings/schema", headers={"X-Dashboard-Token": "supersecret"})
    assert r2.status_code == 200


def test_auth_rejects_cookie_only(monkeypatch: pytest.MonkeyPatch, app: FastAPI) -> None:
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

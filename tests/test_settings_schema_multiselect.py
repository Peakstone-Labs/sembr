"""D13: NEWSAPI_CATEGORIES is exposed as type='multiselect' with the 8-candidate enum."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api import settings as settings_router_mod
from sembr.api.settings import _MULTISELECT_FIELDS, router
from sembr.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    p = tmp_path / ".env"
    p.write_text("QDRANT_URL=http://qdrant:6333\n", encoding="utf-8")
    monkeypatch.setattr(settings_router_mod, "ENV_FILE_PATH", p)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_newsapi_categories_emits_multiselect(client: TestClient) -> None:
    r = client.get("/api/settings/schema")
    assert r.status_code == 200
    fields = {f["key"]: f for f in r.json()["sembr_fields"]}
    assert "NEWSAPI_CATEGORIES" in fields
    f = fields["NEWSAPI_CATEGORIES"]
    assert f["type"] == "multiselect"
    assert f["enum"] == _MULTISELECT_FIELDS["newsapi_categories"]
    assert len(f["enum"]) == 8


def test_other_str_fields_unchanged(client: TestClient) -> None:
    """Sanity: existing CSV fields like PROXY_HOSTS stay 'str' — only the
    explicitly-listed multiselect names get the override."""
    r = client.get("/api/settings/schema")
    fields = {f["key"]: f for f in r.json()["sembr_fields"]}
    assert fields["PROXY_HOSTS"]["type"] == "str"


def test_newsapi_api_key_is_secret(client: TestClient) -> None:
    r = client.get("/api/settings/schema")
    fields = {f["key"]: f for f in r.json()["sembr_fields"]}
    f = fields["NEWSAPI_API_KEY"]
    assert f["sensitive"] is True
    assert f["type"] == "secret"

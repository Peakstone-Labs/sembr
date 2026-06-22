# SPDX-License-Identifier: Apache-2.0
"""Tests for the map sub-feature endpoints + runner.

- POST /api/intents/{id}/history/{row_id}/extract-sources (404/422/409/202)
- GET  /api/intents/{id}/extract-sources/{task_id}        (404/200)
- GET  /api/intents/{id}/extractions/{article_id}         (404/422/200)
- 401 e2e for all three (DashboardToken middleware, /api/* → 401 not 302)
- run_extract_sources over a real in-memory DB + fake Qdrant + fake LLM
  (happy / cache-skip / override / expired-body).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.config import get_settings
from sembr.dashboard.auth import DashboardTokenMiddleware
from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.mr_cache import get_extraction, init_mr_cache_tables
from sembr.db.sqlite import install_for_test
from sembr.models import IntentCreate
from sembr.summarizer.llm.base import BaseLLMBackend
from sembr.summarizer.mr_extract import run_extract_sources
from sembr.summarizer.mr_extract_tasks import (
    _reset_for_testing,
    try_acquire_row,
)
from sembr.summarizer.mr_extract_tasks import (
    create_task as create_extract_task,
)
from sembr.summarizer.spec import compile_validator, load_spec

_REPO_PROMPTS = Path(__file__).resolve().parents[1] / "prompts"
_SPEC = load_spec("fed_watch", prompts_dir=_REPO_PROMPTS)
_AID = "11111111-1111-1111-1111-111111111111"
_LEGAL = {
    "source_org": "德意志银行",
    "thesis": "t",
    "claims": [{"section": "data_release", "text": "x", "quote": "q", "indicator": "CPI"}],
}


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_testing()
    yield
    _reset_for_testing()


# --------------------------------------------------------------------------- #
# Fakes for the runner
# --------------------------------------------------------------------------- #
class _FakeLLM(BaseLLMBackend):
    def __init__(self, reply: str) -> None:
        self._reply = reply

    @property
    def max_prompt_chars(self) -> int:
        return 10_000

    async def summarize(self, prompt, *, system=None):  # pragma: no cover
        raise NotImplementedError

    async def chat(self, prompt, *, system=None, model=None, json_mode=False):
        return self._reply

    async def health(self):  # pragma: no cover
        return True


class _FakePoint:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


class _FakeQdrant:
    """retrieve() returns a point per id whose payload is non-None in the map."""

    def __init__(self, by_id: dict) -> None:
        self._by_id = by_id

    async def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        return [_FakePoint(self._by_id[str(i)]) for i in ids if self._by_id.get(str(i)) is not None]


def _fake_app(qdrant, llm, reduce_concurrency=16) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            qdrant=SimpleNamespace(client=qdrant),
            llm_backend=llm,
            settings=SimpleNamespace(
                effective_reduce_model="reduce-x",
                reduce_concurrency=reduce_concurrency,
            ),
        )
    )


def _intent_create() -> IntentCreate:
    return IntentCreate(
        name="mr", text="Fed", channels=[{"type": "email", "to": ["a@example.com"]}]
    )


# --------------------------------------------------------------------------- #
# Endpoint tests (mocked deps, router only)
# --------------------------------------------------------------------------- #
def _make_app() -> FastAPI:
    from sembr.api.history import router

    app = FastAPI()
    app.include_router(router)
    return app


def _intent_obj():
    from datetime import UTC, datetime

    from sembr.models import CronSchedule, Intent

    return Intent(
        id=1,
        name="i",
        text="Fed",
        threshold=0.75,
        enabled=True,
        channels=[],
        tags=[],
        schedule=CronSchedule(preset="daily"),
        system_template="fed_watch",
        instruction_template="default",
        extraction_enabled=False,
        feed_filter=None,
        timezone="UTC",
        language="zh",
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


def test_post_extract_404_intent():
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=None)),
    ):
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/intents/1/history/5/extract-sources"
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "intent not found"


def test_post_extract_404_row():
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_intent_obj())),
        patch("sembr.api.history.get_summary", new=AsyncMock(return_value=None)),
    ):
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/intents/1/history/5/extract-sources"
        )
    assert r.status_code == 404
    assert r.json()["detail"] == "history row not found"


def test_post_extract_422_spec_not_found():
    from sembr.summarizer.spec import SpecNotFoundError

    app = _make_app()
    row = {"citations": [{"article_id": _AID}]}
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_intent_obj())),
        patch("sembr.api.history.get_summary", new=AsyncMock(return_value=row)),
        patch("sembr.api.history.load_spec", side_effect=SpecNotFoundError("missing")),
    ):
        r = TestClient(app, raise_server_exceptions=False).post(
            "/api/intents/1/history/5/extract-sources"
        )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "spec_not_found"


def test_post_extract_202_and_409_in_progress():
    app = _make_app()
    row = {"citations": [{"article_id": _AID}, {"article_id": "aid-2"}]}
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_intent_obj())),
        patch("sembr.api.history.get_summary", new=AsyncMock(return_value=row)),
        patch("sembr.api.history.load_spec", return_value=_SPEC),
        patch("sembr.api.history.run_extract_sources", new=AsyncMock()),
    ):
        client = TestClient(app, raise_server_exceptions=False)
        r1 = client.post("/api/intents/1/history/5/extract-sources?override=true")
        assert r1.status_code == 202
        body = r1.json()
        assert body["schema_version"] == _SPEC.schema_version
        assert body["total"] == 2
        assert body["status_url"].endswith(f"/api/intents/1/extract-sources/{body['task_id']}")
        # Second concurrent POST on the same row → 409 (lock held by the first).
        r2 = client.post("/api/intents/1/history/5/extract-sources")
        assert r2.status_code == 409
        assert r2.json()["detail"] == "extract_in_progress"


def test_get_extract_status_404_and_200():
    app = _make_app()
    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/api/intents/1/extract-sources/nope").status_code == 404
    task = create_extract_task(intent_id=1, row_id=5, total=3)
    task.progress.done = 2
    task.errors.append({"article_id": "a", "reason": "boom"})
    r = client.get(f"/api/intents/1/extract-sources/{task.task_id}")
    assert r.status_code == 200
    p = r.json()
    assert p["progress"] == {"done": 2, "skipped": 0, "errors": 0, "total": 3}
    assert p["errors"] == [{"article_id": "a", "reason": "boom"}]
    # task scoped to intent: wrong intent_id → 404
    assert client.get(f"/api/intents/2/extract-sources/{task.task_id}").status_code == 404


def test_get_extractions_404_not_extracted_and_200():
    app = _make_app()
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_intent_obj())),
        patch("sembr.api.history.load_spec", return_value=_SPEC),
        patch("sembr.api.history.get_extraction", new=AsyncMock(return_value=None)),
    ):
        r = TestClient(app, raise_server_exceptions=False).get(f"/api/intents/1/extractions/{_AID}")
    assert r.status_code == 404
    assert r.json()["detail"] == "not_extracted"

    record = {"article_id": _AID, "extraction": _LEGAL, "created_at": "2026-06-14T00:00:00"}
    with (
        patch("sembr.api.history.get_conn", return_value=MagicMock()),
        patch("sembr.api.history.get_intent", new=AsyncMock(return_value=_intent_obj())),
        patch("sembr.api.history.load_spec", return_value=_SPEC),
        patch("sembr.api.history.get_extraction", new=AsyncMock(return_value=record)),
    ):
        r = TestClient(app, raise_server_exceptions=False).get(f"/api/intents/1/extractions/{_AID}")
    assert r.status_code == 200
    body = r.json()
    assert body["extraction"]["source_org"] == "德意志银行"
    # spec-driven display map is shipped alongside the extraction
    fm = body["field_meta"]
    assert fm["is_projection"] == {"role": "flag", "label": "Projection", "type": "bool"}
    assert fm["source_type"]["role"] == "meta"
    assert fm["indicator"]["role"] == "content"


# --------------------------------------------------------------------------- #
# 401 e2e — auth middleware gates /api/intents/* with JSON 401 (not a 302)
# --------------------------------------------------------------------------- #
@pytest.fixture
def _token(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret123")
    yield
    get_settings.cache_clear()


def _auth_app() -> FastAPI:
    from sembr.api.history import router

    app = FastAPI()
    app.add_middleware(DashboardTokenMiddleware)
    app.include_router(router)
    return app


def test_extract_endpoints_401_without_token(_token):
    client = TestClient(_auth_app(), follow_redirects=False, raise_server_exceptions=False)
    assert client.post("/api/intents/1/history/5/extract-sources").status_code == 401
    assert client.get("/api/intents/1/extract-sources/abc").status_code == 401
    assert client.get(f"/api/intents/1/extractions/{_AID}").status_code == 401


# --------------------------------------------------------------------------- #
# Runner integration — real in-memory DB + fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_intent():
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_mr_cache_tables(conn)
    install_for_test(conn)
    intent = await create_intent(conn, _intent_create())
    yield conn, intent
    await conn.close()


async def _run(conn, intent, citations, qdrant, *, override=False, row_id=5):
    validator = compile_validator(_SPEC)
    app = _fake_app(qdrant, _FakeLLM(json.dumps(_LEGAL, ensure_ascii=False)))
    assert try_acquire_row(row_id)  # caller acquires per the ownership contract
    task = create_extract_task(intent.id, row_id, total=len(citations))
    await run_extract_sources(
        intent_id=intent.id,
        row_id=row_id,
        override=override,
        citations=citations,
        spec=_SPEC,
        validator=validator,
        schema_version=_SPEC.schema_version,
        intent_text="Fed",
        app=app,
        task=task,
    )
    return task


async def test_runner_happy_populates_cache(db_intent):
    conn, intent = db_intent
    qdrant = _FakeQdrant({_AID: {"body": "real body", "title": "T", "published_at": "2026-06-14"}})
    task = await _run(conn, intent, [{"article_id": _AID, "source_name": "Feed"}], qdrant)
    assert task.status == "done"
    assert (task.progress.done, task.progress.errors, task.progress.skipped) == (1, 0, 0)
    rec = await get_extraction(conn, _AID, intent.id, _SPEC.schema_version)
    assert rec is not None
    assert rec["extraction"]["source_org"] == "德意志银行"
    assert rec["source_name"] == "Feed"
    assert try_acquire_row(5)  # runner released the lock in finally


async def test_runner_skips_cache_hit_then_override_reextracts(db_intent):
    conn, intent = db_intent
    qdrant = _FakeQdrant({_AID: {"body": "real body", "title": "T", "published_at": None}})
    cites = [{"article_id": _AID}]
    await _run(conn, intent, cites, qdrant)  # first run populates
    t2 = await _run(conn, intent, cites, qdrant, row_id=6)  # second, no override
    assert (t2.progress.done, t2.progress.skipped) == (0, 1)  # cache hit → skipped
    t3 = await _run(conn, intent, cites, qdrant, override=True, row_id=7)
    assert (t3.progress.done, t3.progress.skipped) == (1, 0)  # override re-extracts


async def test_runner_uses_settings_concurrency(db_intent, monkeypatch):
    _conn, intent = db_intent
    captured = {}
    real_semaphore = asyncio.Semaphore

    def _spy(n):
        captured["n"] = n
        return real_semaphore(n)

    monkeypatch.setattr("sembr.summarizer.mr_extract.asyncio.Semaphore", _spy)
    qdrant = _FakeQdrant({_AID: {"body": "b", "title": "T", "published_at": None}})
    validator = compile_validator(_SPEC)
    app = _fake_app(qdrant, _FakeLLM(json.dumps(_LEGAL, ensure_ascii=False)), reduce_concurrency=42)
    assert try_acquire_row(9)
    task = create_extract_task(intent.id, 9, total=1)
    await run_extract_sources(
        intent_id=intent.id,
        row_id=9,
        override=False,
        citations=[{"article_id": _AID}],
        spec=_SPEC,
        validator=validator,
        schema_version=_SPEC.schema_version,
        intent_text="Fed",
        app=app,
        task=task,
    )
    assert captured["n"] == 42  # Semaphore sized from settings.reduce_concurrency


async def test_runner_records_expired_body_error(db_intent):
    conn, intent = db_intent
    qdrant = _FakeQdrant({_AID: None})  # not in Qdrant → expired
    task = await _run(conn, intent, [{"article_id": _AID}], qdrant)
    assert task.status == "done"  # batch completes despite the failure
    assert task.progress.errors == 1
    assert task.progress.done == 0
    assert task.errors[0]["article_id"] == _AID
    assert "expired" in task.errors[0]["reason"]
    assert await get_extraction(conn, _AID, intent.id, _SPEC.schema_version) is None

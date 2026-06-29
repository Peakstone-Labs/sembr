# SPDX-License-Identifier: Apache-2.0
"""KB API tests (delta-label/kb SF1, design §6).

Sync TestClient over a bare app mounting only the kb router. The SQLite conn is
created inside the app lifespan (same event loop as requests) to avoid the
cross-loop aiosqlite issue (mirrors tests/test_intents.py). LLM distill is a fake.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import aiosqlite
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.kb import router
from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.sqlite import install_for_test
from sembr.db.summary_history import init_summary_history_table, save_summary
from sembr.kb.gitrepo import GitRepo
from sembr.kb.store import KbStore
from sembr.models import IntentCreate
from sembr.summarizer.models import SummaryResult


class _FakeDistillBackend:
    async def structured(self, prompt, schema, *, system=None, model=None, repair_attempts=2):
        return schema(
            events=[
                {
                    "title": "逆回购利率",
                    "section": "货币政策",
                    "first_seen": "2026-06-01",
                    "last_seen": "2026-06-20",
                    "state": "维持1.50%",
                },
                {
                    "title": "社融",
                    "section": "增长与数据",
                    "first_seen": "2026-06-05",
                    "last_seen": "2026-06-19",
                    "state": "同比多增",
                },
            ]
        )


def _intent_body(name: str) -> IntentCreate:
    return IntentCreate.model_validate(
        {"name": name, "text": "t", "channels": [{"type": "email", "to": ["a@b.c"]}]}
    )


def _client(tmp_path):
    """App with kb router; intent 1 has summary history, intent 2 has none."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await init_summary_history_table(conn)
        install_for_test(conn)
        i1 = await create_intent(conn, _intent_body("with-history"))
        await create_intent(conn, _intent_body("no-history"))
        run_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        await save_summary(
            conn,
            SummaryResult(intent_id=i1.id, summary="## 货币\n- 逆回购维持\n- 社融多增\n"),
            run_at=run_at,
        )
        app.state.kb_store = KbStore(root=tmp_path, git=GitRepo(tmp_path))
        app.state.llm_backend = _FakeDistillBackend()
        app.state.settings = SimpleNamespace(effective_kb_distill_model="fake-pro")
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    return TestClient(app)


def test_get_unbuilt_kb(tmp_path) -> None:
    with _client(tmp_path) as c:
        r = c.get("/api/kb/1/events")
        assert r.status_code == 200
        body = r.json()
        assert body["exists"] is False and body["content"] == ""
        assert body["content_hash"] is None


def test_put_get_roundtrip(tmp_path) -> None:
    content = "## S\n- <!--k:a--> **t**（首见 2026-06-01，最新 2026-06-01）：s\n"
    with _client(tmp_path) as c:
        r = c.put("/api/kb/1/events", json={"content": content})
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        g = c.get("/api/kb/1/events").json()
        assert g["exists"] is True and g["content"] == content
        assert g["content_hash"]


def test_put_optimistic_conflict_409(tmp_path) -> None:
    with _client(tmp_path) as c:
        c.put("/api/kb/1/events", json={"content": "## S\n- x\n"})
        r = c.put("/api/kb/1/events", json={"content": "## S\n- y\n", "base_hash": "stalehash123"})
        assert r.status_code == 409


def test_put_oversize_413(tmp_path) -> None:
    with _client(tmp_path) as c:
        r = c.put("/api/kb/1/events", json={"content": "x" * (256 * 1024 + 1)})
        assert r.status_code == 413


def test_put_key_integrity_warnings(tmp_path) -> None:
    content = "## S\n- this bullet has no key anchor\n"
    with _client(tmp_path) as c:
        r = c.put("/api/kb/1/events", json={"content": content})
        assert r.status_code == 200
        assert r.json()["warnings"]  # non-empty warnings list


def test_invalid_kind_400(tmp_path) -> None:
    with _client(tmp_path) as c:
        assert c.get("/api/kb/1/playbook").status_code == 400


def test_unknown_intent_404(tmp_path) -> None:
    with _client(tmp_path) as c:
        assert c.get("/api/kb/999/events").status_code == 404


def test_rebuild_creates_and_confirm_gate(tmp_path) -> None:
    with _client(tmp_path) as c:
        # intent 1 has history → first rebuild succeeds.
        r = c.post("/api/kb/1/rebuild", json={})
        assert r.status_code == 200, r.text
        assert r.json()["events"] == 2
        # second rebuild without confirm → 409 (would overwrite).
        assert c.post("/api/kb/1/rebuild", json={}).status_code == 409
        # with confirm → 200.
        assert c.post("/api/kb/1/rebuild", json={"confirm": True}).status_code == 200


def test_rebuild_no_history_422(tmp_path) -> None:
    with _client(tmp_path) as c:
        assert c.post("/api/kb/2/rebuild", json={}).status_code == 422


def test_rebuild_inflight_409(tmp_path) -> None:
    """Review 🟡-1: a concurrent rebuild is rejected (no double pro distill)."""
    with _client(tmp_path) as c:
        c.app.state.kb_store.try_begin_rebuild(1)  # simulate one in flight
        r = c.post("/api/kb/1/rebuild", json={"confirm": True})
        assert r.status_code == 409
        c.app.state.kb_store.end_rebuild(1)


def test_manual_lint(tmp_path) -> None:
    with _client(tmp_path) as c:
        # not built yet → 409.
        assert c.post("/api/kb/1/lint").status_code == 409
        # build it, then lint succeeds with a stats payload.
        c.post("/api/kb/1/rebuild", json={})
        r = c.post("/api/kb/1/lint")
        assert r.status_code == 200
        assert set(r.json()) == {"merged_dups", "archived", "marked", "empty_sections"}

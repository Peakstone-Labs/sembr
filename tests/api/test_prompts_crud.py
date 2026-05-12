# SPDX-License-Identifier: Apache-2.0
"""End-to-end CRUD tests for /api/prompts/templates.

Exercises list, create, update, delete, rename via TestClient against an
in-memory aiosqlite. The filesystem layer is the actual on-disk one
(tmp_path) — atomic write semantics and TOCTOU pre-check are verified for
real, not mocked.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.prompts import router as prompts_router
from sembr.db.intents import init_intent_tables
from sembr.db.sqlite import install_for_test


@contextmanager
def _client(prompts_dir: Path):
    conn_holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(prompts_router)
    app.state.settings = MagicMock()

    with patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir):
        with TestClient(app) as http:
            yield http, conn_holder


@pytest.fixture()
def prompts_dir(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "instruction").mkdir()
    (tmp_path / "system" / "default.md").write_text("Lang: {language}", encoding="utf-8")
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )
    return tmp_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed_intent_async(
    conn: aiosqlite.Connection,
    name: str,
    *,
    system_template: str = "default",
    instruction_template: str = "default",
) -> int:
    cursor = await conn.execute(
        """INSERT INTO intents
               (name, text, channels, tags, system_template, instruction_template,
                feed_filter, schedule, timezone, language, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            "x",
            json.dumps([{"type": "email", "to": ["a@b.com"]}]),
            json.dumps([]),
            system_template,
            instruction_template,
            "null",
            json.dumps(
                {
                    "mode": "cron",
                    "preset": "daily",
                    "hour": 9,
                    "minute": 0,
                    "lookback_seconds": 86400,
                    "skip_seen": True,
                }
            ),
            "UTC",
            "zh",
            _now(),
            _now(),
        ),
    )
    await conn.commit()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_intent_via(
    conn_holder, name, *, system_template="default", instruction_template="default"
):
    """Sync wrapper using the TestClient's event loop."""
    import asyncio  # noqa: PLC0415

    return asyncio.get_event_loop().run_until_complete(
        _seed_intent_async(
            conn_holder["conn"],
            name,
            system_template=system_template,
            instruction_template=instruction_template,
        )
    )


# ---------------------------------------------------------------------------
# SC #1 — fresh dir lists only `default` × 2 with is_builtin=true, ref_count=0
# ---------------------------------------------------------------------------


def test_list_empty_dir_returns_only_default(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        resp = http.get("/api/prompts/templates")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["system"]) == 1
    assert len(data["instruction"]) == 1
    sys_default = data["system"][0]
    assert sys_default["name"] == "default"
    assert sys_default["is_builtin"] is True
    assert sys_default["ref_count"] == 0
    assert sys_default["ref_intents"] == []


# ---------------------------------------------------------------------------
# SC #2 — POST seed-from-default
# ---------------------------------------------------------------------------


def test_create_seeds_from_default(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        resp = http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
    assert resp.status_code == 201, resp.text
    new_path = prompts_dir / "instruction" / "crypto_zh.md"
    assert new_path.exists()
    assert new_path.read_text(encoding="utf-8") == "Topic: {intent_text}\n{articles}"
    body = resp.json()
    assert body["name"] == "crypto_zh"
    assert body["is_builtin"] is False
    assert body["ref_count"] == 0


# ---------------------------------------------------------------------------
# SC #3 — PUT updates content; size + mtime non-decreasing
# ---------------------------------------------------------------------------


def test_put_updates_content_and_size(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        # Seed a non-builtin we can edit
        seed = http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        assert seed.status_code == 201
        old_mtime = seed.json()["mtime"]

        # Linux fs mtime resolution can be 1ns or coarser; sleep a hair to make >= unambiguous.
        time.sleep(0.01)

        new_content = "Topic: {intent_text}\n{articles}\nNew note."
        resp = http.put(
            "/api/prompts/templates/instruction/crypto_zh",
            json={"content": new_content},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["size_bytes"] == len(new_content.encode("utf-8"))
    assert body["mtime"] >= old_mtime  # mtime must be non-decreasing across writes
    assert (prompts_dir / "instruction" / "crypto_zh.md").read_text(encoding="utf-8") == new_content


# ---------------------------------------------------------------------------
# SC #4 — PUT with unknown placeholder → 422; file unchanged
# ---------------------------------------------------------------------------


def test_put_unknown_placeholder_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        before = (prompts_dir / "instruction" / "crypto_zh.md").read_text(encoding="utf-8")
        resp = http.put(
            "/api/prompts/templates/instruction/crypto_zh",
            json={"content": "Topic: {intent}\n{articles}"},  # {intent} unknown
        )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["field"] == "content"
    assert "intent" in detail["reason"]
    # File unchanged
    after = (prompts_dir / "instruction" / "crypto_zh.md").read_text(encoding="utf-8")
    assert before == after


# ---------------------------------------------------------------------------
# SC #5 — DELETE referenced template → 409 with ref_intents
# ---------------------------------------------------------------------------


def test_delete_referenced_returns_409(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, conn_holder):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        intent_id = _seed_intent_via(conn_holder, "owner", instruction_template="crypto_zh")

        resp = http.delete("/api/prompts/templates/instruction/crypto_zh")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["ref_count"] == 1
    assert detail["ref_intents"] == [{"id": intent_id, "name": "owner"}]
    assert (prompts_dir / "instruction" / "crypto_zh.md").exists()


# ---------------------------------------------------------------------------
# SC #6 — Rename cascades to intents
# ---------------------------------------------------------------------------


def test_rename_cascades_to_intents(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, conn_holder):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        i1 = _seed_intent_via(conn_holder, "alpha", instruction_template="crypto_zh")
        i2 = _seed_intent_via(conn_holder, "beta", instruction_template="crypto_zh")

        resp = http.post(
            "/api/prompts/templates/instruction/crypto_zh/rename",
            json={"new_name": "crypto_zh_v2"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "crypto_zh_v2"
    # Both intents now reference the new name (ref_count == 2)
    assert body["ref_count"] == 2
    assert {(r["id"], r["name"]) for r in body["ref_intents"]} == {(i1, "alpha"), (i2, "beta")}
    # Filesystem moved
    assert (prompts_dir / "instruction" / "crypto_zh_v2.md").exists()
    assert not (prompts_dir / "instruction" / "crypto_zh.md").exists()


# ---------------------------------------------------------------------------
# SC #7 — Reserved name `default` rejected on create + rename (422)
# ---------------------------------------------------------------------------


def test_create_named_default_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        # Create
        resp = http.post("/api/prompts/templates/instruction", json={"name": "default"})
        assert resp.status_code == 422
        # Rename to default (after first creating a non-builtin that can be renamed)
        http.post("/api/prompts/templates/instruction", json={"name": "scratch"})
        rename_resp = http.post(
            "/api/prompts/templates/instruction/scratch/rename",
            json={"new_name": "default"},
        )
    assert rename_resp.status_code == 422


# ---------------------------------------------------------------------------
# SC #8 — Builtin write/delete/rename → 403
# ---------------------------------------------------------------------------


def test_default_writes_return_403(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        put_resp = http.put(
            "/api/prompts/templates/system/default",
            json={"content": "Lang: {language}\nedited"},
        )
        del_resp = http.delete("/api/prompts/templates/system/default")
        rename_resp = http.post(
            "/api/prompts/templates/system/default/rename",
            json={"new_name": "default_v2"},
        )
    assert put_resp.status_code == 403
    assert del_resp.status_code == 403
    assert rename_resp.status_code == 403


# ---------------------------------------------------------------------------
# SC #9 — POST with explicit source duplicates an existing template
# ---------------------------------------------------------------------------


def test_duplicate_seeds_from_existing(prompts_dir: Path) -> None:
    custom_content = "Custom topic: {intent_text}\n{articles}\nfoo"
    (prompts_dir / "instruction" / "crypto_zh.md").write_text(custom_content, encoding="utf-8")

    with _client(prompts_dir) as (http, _):
        resp = http.post(
            "/api/prompts/templates/instruction",
            json={"name": "crypto_en", "source": "crypto_zh"},
        )
    assert resp.status_code == 201, resp.text
    assert (prompts_dir / "instruction" / "crypto_en.md").read_text(
        encoding="utf-8"
    ) == custom_content


# ---------------------------------------------------------------------------
# SC #10 — Oversize and empty content both 422
# ---------------------------------------------------------------------------


def test_oversize_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        oversize = http.put(
            "/api/prompts/templates/instruction/crypto_zh",
            json={"content": "x" * 65537},
        )
        empty = http.put(
            "/api/prompts/templates/instruction/crypto_zh",
            json={"content": ""},
        )
    assert oversize.status_code == 422
    assert empty.status_code == 422


# ---------------------------------------------------------------------------
# Additional: target collision on POST
# ---------------------------------------------------------------------------


def test_create_existing_target_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        http.post("/api/prompts/templates/instruction", json={"name": "scratch"})
        resp = http.post("/api/prompts/templates/instruction", json={"name": "scratch"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "name"


def test_create_missing_source_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        resp = http.post(
            "/api/prompts/templates/instruction",
            json={"name": "new_one", "source": "ghost"},
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "source"


# ---------------------------------------------------------------------------
# Additional: rename to existing target → 422 (pre-check)
# ---------------------------------------------------------------------------


def test_rename_existing_target_returns_422(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        http.post("/api/prompts/templates/instruction", json={"name": "a"})
        http.post("/api/prompts/templates/instruction", json={"name": "b"})
        resp = http.post(
            "/api/prompts/templates/instruction/a/rename",
            json={"new_name": "b"},
        )
    assert resp.status_code == 422
    # Files unchanged
    assert (prompts_dir / "instruction" / "a.md").exists()
    assert (prompts_dir / "instruction" / "b.md").exists()


# ---------------------------------------------------------------------------
# Additional: invalid kind → 400 on every endpoint
# ---------------------------------------------------------------------------


def test_invalid_kind_400(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        assert http.post("/api/prompts/templates/badkind", json={"name": "x"}).status_code == 400
        assert (
            http.put("/api/prompts/templates/badkind/x", json={"content": "y"}).status_code == 400
        )
        assert http.delete("/api/prompts/templates/badkind/x").status_code == 400
        assert (
            http.post("/api/prompts/templates/badkind/x/rename", json={"new_name": "y"}).status_code
            == 400
        )

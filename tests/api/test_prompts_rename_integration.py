# SPDX-License-Identifier: Apache-2.0
"""Integration tests for the rename endpoint (D2 / D15).

These exercise the full 3-step orchestration in `rename_template_endpoint`:
filesystem `os.rename` → SQLite cascade UPDATE inside `db.transaction()`,
plus the reverse-rename rollback path when the UPDATE raises.

Distinct from `test_prompts_crud.py`'s SC#6 happy path: this file targets
the cross-boundary atomicity guarantees (file + DB stay in sync, even on
the SQLite-side failure path).
"""

from __future__ import annotations

import json
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
    (tmp_path / "instruction" / "default.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )
    return tmp_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _seed(conn: aiosqlite.Connection, name: str, *, instruction_template: str) -> int:
    cur = await conn.execute(
        """INSERT INTO intents
               (name, text, channels, tags, system_template, instruction_template,
                feed_filter, schedule, timezone, language, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name,
            "x",
            json.dumps([{"type": "email", "to": ["a@b.com"]}]),
            json.dumps([]),
            "default",
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
    assert cur.lastrowid is not None
    return cur.lastrowid


def _seed_via(conn_holder, name, *, instruction_template):
    import asyncio  # noqa: PLC0415

    return asyncio.get_event_loop().run_until_complete(
        _seed(conn_holder["conn"], name, instruction_template=instruction_template)
    )


# ---------------------------------------------------------------------------
# Happy path — file moves AND every referencing intent's column updates
# ---------------------------------------------------------------------------


def test_rename_e2e_file_and_intents(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, conn_holder):
        # Seed a non-builtin
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})

        # Seed 2 referencing intents + 1 unrelated
        i1 = _seed_via(conn_holder, "alpha", instruction_template="crypto_zh")
        i2 = _seed_via(conn_holder, "beta", instruction_template="crypto_zh")
        i3 = _seed_via(conn_holder, "gamma", instruction_template="default")

        # Single-request rename
        resp = http.post(
            "/api/prompts/templates/instruction/crypto_zh/rename",
            json={"new_name": "crypto_zh_v2"},
        )
        assert resp.status_code == 200, resp.text

        # Filesystem moved
        assert (prompts_dir / "instruction" / "crypto_zh_v2.md").is_file()
        assert not (prompts_dir / "instruction" / "crypto_zh.md").exists()

        # Intent rows updated
        list_resp = http.get("/api/prompts/templates")
        assert list_resp.status_code == 200
        rows = {r["name"]: r for r in list_resp.json()["instruction"]}
        assert "crypto_zh_v2" in rows
        assert "crypto_zh" not in rows
        v2 = rows["crypto_zh_v2"]
        assert v2["ref_count"] == 2
        assert {(r["id"], r["name"]) for r in v2["ref_intents"]} == {
            (i1, "alpha"),
            (i2, "beta"),
        }
        # Unrelated intent (`gamma`) still on `default`
        default_row = rows["default"]
        gamma_ids = {r["id"] for r in default_row["ref_intents"]}
        assert i3 in gamma_ids


# ---------------------------------------------------------------------------
# Reverse-rollback — when the SQLite UPDATE raises, the file rename reverts
# ---------------------------------------------------------------------------


def test_rename_db_failure_reverts_filesystem(prompts_dir: Path) -> None:
    """If `rename_intent_template` raises, the API must reverse `os.rename`.

    Patches `sembr.api.prompts.rename_intent_template` to raise; expects 500
    AND the original file at the old path AND no file at the new path.
    """
    with _client(prompts_dir) as (http, conn_holder):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        _seed_via(conn_holder, "alpha", instruction_template="crypto_zh")

        # Force DB step to raise
        async def boom(*_args, **_kwargs):
            raise RuntimeError("simulated SQLite failure")

        with patch("sembr.api.prompts.rename_intent_template", side_effect=boom):
            resp = http.post(
                "/api/prompts/templates/instruction/crypto_zh/rename",
                json={"new_name": "crypto_zh_v2"},
            )
        assert resp.status_code == 500
        # Reverse-rename succeeded → original file still there, no new file
        assert (prompts_dir / "instruction" / "crypto_zh.md").is_file()
        assert not (prompts_dir / "instruction" / "crypto_zh_v2.md").exists()
        # And the intent's column is unchanged
        list_resp = http.get("/api/prompts/templates")
        rows = {r["name"]: r for r in list_resp.json()["instruction"]}
        assert "crypto_zh" in rows
        assert rows["crypto_zh"]["ref_count"] == 1


# ---------------------------------------------------------------------------
# No-op rename (new == old) returns 200 without filesystem churn
# ---------------------------------------------------------------------------


def test_rename_to_same_name_is_noop(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        http.post("/api/prompts/templates/instruction", json={"name": "crypto_zh"})
        before_mtime = (prompts_dir / "instruction" / "crypto_zh.md").stat().st_mtime
        resp = http.post(
            "/api/prompts/templates/instruction/crypto_zh/rename",
            json={"new_name": "crypto_zh"},
        )
        assert resp.status_code == 200
        # File mtime should be unchanged (we did not actually rename)
        after_mtime = (prompts_dir / "instruction" / "crypto_zh.md").stat().st_mtime
        assert after_mtime == before_mtime


# ---------------------------------------------------------------------------
# 404 on rename of nonexistent source
# ---------------------------------------------------------------------------


def test_rename_nonexistent_source_404(prompts_dir: Path) -> None:
    with _client(prompts_dir) as (http, _):
        resp = http.post(
            "/api/prompts/templates/instruction/ghost/rename",
            json={"new_name": "ghost_v2"},
        )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cancellation arm — `asyncio.CancelledError` from the SQLite step must
# best-effort reverse-rename and propagate (not be swallowed by HTTPException).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_cancellation_reverts_filesystem_and_propagates(
    prompts_dir: Path, tmp_path: Path
) -> None:
    """If `rename_intent_template` raises `CancelledError`, the API reverses the
    filesystem rename AND re-raises the cancellation (does not return 500).

    Tests the endpoint coroutine directly — the FastAPI TestClient/Starlette
    portal translates cancellation to a transport-level error which makes
    HTTP-level assertion noisy. Calling the route function bypasses that
    translation and exercises only the except-clause path under review."""
    import asyncio  # noqa: PLC0415

    from sembr.api.prompts import (  # noqa: PLC0415
        TemplateRenameRequest,
        rename_template_endpoint,
    )

    # Pre-seed: a renamable template + an intent referencing it
    (prompts_dir / "instruction" / "crypto_zh.md").write_text(
        "Topic: {intent_text}\n{articles}", encoding="utf-8"
    )

    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    install_for_test(conn)
    try:
        await _seed(conn, "alpha", instruction_template="crypto_zh")

        async def cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError()

        request = MagicMock()
        body = TemplateRenameRequest(new_name="crypto_zh_v2")

        with (
            patch("sembr.summarizer.templates.PROMPTS_DIR", prompts_dir),
            patch("sembr.api.prompts.rename_intent_template", side_effect=cancelled),
        ):
            with pytest.raises(asyncio.CancelledError):
                await rename_template_endpoint("instruction", "crypto_zh", body, request)

        # File reverted to the old path; new path absent
        assert (prompts_dir / "instruction" / "crypto_zh.md").is_file()
        assert not (prompts_dir / "instruction" / "crypto_zh_v2.md").exists()
    finally:
        await conn.close()

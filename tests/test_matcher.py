# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the reverse-rag feature.

Covers:
  - sembr/db/match_seen.py CRUD
  - sembr/matcher/scan.py run_intent_scan logic
  - sembr/embedder/scheduler.py _to_point ingested_at_ts field
  - lifespan startup: register_all_enabled called for enabled intents
  - match_seen ON DELETE CASCADE (non-SC test from design)
  - POST register_job failure rollback (non-SC test from design)

All tests are Windows-runnable (no Docker, no qdrant_client runtime needed).
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest

from sembr.db.intents import create_intent, init_intent_tables
from sembr.db.match_seen import clear_intent, init_match_seen_tables, insert_unseen_returning_new
from sembr.db.sqlite import install_for_test
from sembr.models import IntentCreate

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def mem_conn():
    """In-memory SQLite connection with intents + match_seen tables."""
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    await init_intent_tables(conn)
    await init_match_seen_tables(conn)
    install_for_test(conn)
    yield conn
    await conn.close()


_INTENT_BODY = IntentCreate(
    name="test-intent",
    text="market movements",
    channels=[{"type": "email", "to": ["a@example.com"]}],
)


# ---------------------------------------------------------------------------
# match_seen CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_unseen_returning_new_all_new(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)
    article_ids = ["aa", "bb", "cc"]

    new_ids = await insert_unseen_returning_new(mem_conn, intent.id, article_ids)
    assert set(new_ids) == set(article_ids)


@pytest.mark.asyncio
async def test_insert_unseen_returning_new_dedup(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)
    await insert_unseen_returning_new(mem_conn, intent.id, ["aa", "bb"])

    new_ids = await insert_unseen_returning_new(mem_conn, intent.id, ["bb", "cc"])
    assert set(new_ids) == {"cc"}  # bb already seen, aa not re-inserted


@pytest.mark.asyncio
async def test_insert_unseen_returning_new_empty(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)
    result = await insert_unseen_returning_new(mem_conn, intent.id, [])
    assert result == []


@pytest.mark.asyncio
async def test_clear_intent_removes_rows(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)
    await insert_unseen_returning_new(mem_conn, intent.id, ["x1", "x2", "x3"])

    await clear_intent(mem_conn, intent.id)

    async with mem_conn.execute(
        "SELECT COUNT(*) FROM match_seen WHERE intent_id=?", (intent.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_on_delete_cascade(mem_conn) -> None:
    """Deleting an intent row cascades to match_seen (non-SC design test)."""
    intent = await create_intent(mem_conn, _INTENT_BODY)
    await insert_unseen_returning_new(mem_conn, intent.id, ["z1", "z2"])

    await mem_conn.execute("DELETE FROM intents WHERE id=?", (intent.id,))
    await mem_conn.commit()

    async with mem_conn.execute(
        "SELECT COUNT(*) FROM match_seen WHERE intent_id=?", (intent.id,)
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# embedder/scheduler._to_point: ingested_at_ts field
# ---------------------------------------------------------------------------


def test_to_point_includes_ingested_at_ts() -> None:
    from sembr.db.articles import PendingRow
    from sembr.embedder.scheduler import _to_point

    row = PendingRow(
        md5="a" * 32,
        url="https://example.com",
        title="title",
        body="body",
        published_at="2026-01-01T00:00:00Z",
        feed_id=1,
        retry_count=0,
    )
    before_ts = int(datetime.now(UTC).timestamp())
    point = _to_point(row, [0.1] * 1024, "bge-m3_v1")
    after_ts = int(datetime.now(UTC).timestamp())

    assert "ingested_at_ts" in point.payload
    ts = point.payload["ingested_at_ts"]
    assert isinstance(ts, int)
    assert before_ts <= ts <= after_ts


# ---------------------------------------------------------------------------
# run_intent_scan: embedder-not-ready guard (SC#11)
# ---------------------------------------------------------------------------


def _make_app(
    *,
    embedder_loaded: bool = True,
    qdrant_client: MagicMock | None = None,
    on_match=None,
) -> MagicMock:
    app = MagicMock()
    app.state.embedder.is_loaded = embedder_loaded
    app.state.qdrant.client = qdrant_client or AsyncMock()
    app.state.on_match = on_match
    return app


@pytest.mark.asyncio
async def test_scan_proceeds_when_embedder_not_ready(mem_conn) -> None:
    # SC#11 behaviour: scan_once uses pre-computed Qdrant vectors, not the
    # embedder. Skipping caused permanent silent misses when the SiliconFlow
    # probe failed at startup (is_loaded stays False indefinitely).
    # Contract: scan proceeds past the embedder check (reaches Qdrant) and
    # on_match is not called when no articles match.
    intent = await create_intent(mem_conn, _INTENT_BODY)
    on_match = AsyncMock()
    qdrant_client = AsyncMock()
    qdrant_client.retrieve = AsyncMock(return_value=[])  # no intent vector → early-return
    app = _make_app(embedder_loaded=False, qdrant_client=qdrant_client, on_match=on_match)

    with patch("sembr.matcher.scan.get_conn", return_value=mem_conn):
        from sembr.matcher.scan import run_intent_scan

        await run_intent_scan(intent.id, app)

    qdrant_client.retrieve.assert_awaited()  # proves scan reached Qdrant despite embedder not loaded
    on_match.assert_not_called()


# ---------------------------------------------------------------------------
# run_intent_scan: Qdrant error skips tick (SC#12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_skips_on_qdrant_error(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)

    mock_client = AsyncMock()
    mock_client.retrieve.side_effect = ConnectionError("qdrant down")
    on_match = AsyncMock()
    app = _make_app(qdrant_client=mock_client, on_match=on_match)

    qdrant_models = MagicMock()
    with (
        patch("sembr.matcher.scan.get_conn", return_value=mem_conn),
        patch.dict(
            "sys.modules",
            {"qdrant_client": MagicMock(), "qdrant_client.models": qdrant_models},
        ),
    ):
        from sembr.matcher.scan import run_intent_scan

        await run_intent_scan(intent.id, app)

    on_match.assert_not_called()
    async with mem_conn.execute("SELECT COUNT(*) FROM match_seen") as cur:
        row = await cur.fetchone()
    assert row[0] == 0


# ---------------------------------------------------------------------------
# run_intent_scan: happy path — new articles trigger callback (SC#1-like unit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_happy_path_triggers_callback(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)

    article_uuid = "11111111-1111-1111-1111-111111111111"
    hit = MagicMock()
    hit.id = article_uuid
    hit.score = 0.82
    hit.payload = {
        "title": "Fed Rate Cut",
        "ingested_at_ts": int(datetime.now(UTC).timestamp()),
        "enabled": True,
    }

    mock_retrieve_result = [MagicMock()]
    mock_retrieve_result[0].vector = {"main": [0.1] * 1024}

    mock_response = MagicMock()
    mock_response.points = [hit]
    mock_client = AsyncMock()
    mock_client.retrieve = AsyncMock(return_value=mock_retrieve_result)
    mock_client.query_points = AsyncMock(return_value=mock_response)

    on_match = AsyncMock()
    app = _make_app(qdrant_client=mock_client, on_match=on_match)

    qdrant_models = MagicMock()

    # patch.dict(sys.modules) makes the lazy `from qdrant_client.models import ...`
    # inside run_intent_scan resolve to the mock, so no real qdrant_client is needed.
    with (
        patch("sembr.matcher.scan.get_conn", return_value=mem_conn),
        patch.dict(
            "sys.modules", {"qdrant_client": MagicMock(), "qdrant_client.models": qdrant_models}
        ),
    ):
        from sembr.matcher.scan import run_intent_scan

        await run_intent_scan(intent.id, app)

    on_match.assert_called_once()
    matches = on_match.call_args.args[0]
    assert len(matches) == 1
    assert matches[0].intent_id == intent.id
    assert matches[0].article_id == article_uuid
    assert matches[0].score == pytest.approx(0.82)


# ---------------------------------------------------------------------------
# run_intent_scan: dedup — second tick on same articles does not re-trigger (SC#2-like)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_dedup_no_repeated_callback(mem_conn) -> None:
    intent = await create_intent(mem_conn, _INTENT_BODY)
    article_uuid = "22222222-2222-2222-2222-222222222222"

    # Pre-seed match_seen as if first tick already ran
    await insert_unseen_returning_new(mem_conn, intent.id, [article_uuid])

    hit = MagicMock()
    hit.id = article_uuid
    hit.score = 0.85
    hit.payload = {"enabled": True}

    mock_response = MagicMock()
    mock_response.points = [hit]
    mock_retrieve_result = [MagicMock()]
    mock_retrieve_result[0].vector = {"main": [0.1] * 1024}
    mock_client = AsyncMock()
    mock_client.retrieve = AsyncMock(return_value=mock_retrieve_result)
    mock_client.query_points = AsyncMock(return_value=mock_response)

    on_match = AsyncMock()
    app = _make_app(qdrant_client=mock_client, on_match=on_match)

    qdrant_models = MagicMock()
    with (
        patch("sembr.matcher.scan.get_conn", return_value=mem_conn),
        patch.dict(
            "sys.modules", {"qdrant_client": MagicMock(), "qdrant_client.models": qdrant_models}
        ),
    ):
        from sembr.matcher.scan import run_intent_scan

        await run_intent_scan(intent.id, app)

    on_match.assert_not_called()


# ---------------------------------------------------------------------------
# lifespan startup: register_all_enabled wires up jobs for enabled intents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_all_enabled_wires_jobs(mem_conn) -> None:
    from sembr.matcher.jobs import register_all_enabled

    intent1 = await create_intent(mem_conn, _INTENT_BODY)
    await create_intent(
        mem_conn,
        IntentCreate(
            name="disabled-intent",
            text="other topic",
            channels=[{"type": "email", "to": ["a@example.com"]}],
            enabled=False,
        ),
    )

    mock_scheduler = MagicMock()
    app = MagicMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.retrieve = AsyncMock(return_value=[MagicMock()])  # vector exists

    from sembr.db.intents import list_intents

    enabled = await list_intents(mem_conn, enabled=True)

    with patch("sembr.matcher.jobs.register_intent_job") as mock_reg:
        await register_all_enabled(mock_scheduler, enabled, app, mock_qdrant)

    # Only the enabled intent should be registered
    assert mock_reg.call_count == 1
    assert mock_reg.call_args.args[1].id == intent1.id


@pytest.mark.asyncio
async def test_register_all_enabled_skips_vector_less_intent(mem_conn) -> None:
    """Intents whose Qdrant vector is missing (partial DELETE failure) must be skipped."""
    from sembr.matcher.jobs import register_all_enabled

    _intent = await create_intent(mem_conn, _INTENT_BODY)

    mock_scheduler = MagicMock()
    app = MagicMock()
    mock_qdrant = AsyncMock()
    mock_qdrant.retrieve = AsyncMock(return_value=[])  # no vector

    from sembr.db.intents import list_intents

    enabled = await list_intents(mem_conn, enabled=True)

    with patch("sembr.matcher.jobs.register_intent_job") as mock_reg:
        await register_all_enabled(mock_scheduler, enabled, app, mock_qdrant)

    mock_reg.assert_not_called()


# ---------------------------------------------------------------------------
# POST rollback: register_job failure → Qdrant + SQLite both rolled back
# ---------------------------------------------------------------------------


def test_post_intent_rollback_on_register_job_failure() -> None:
    """If register_intent_job raises after Qdrant upsert, both are rolled back."""
    from contextlib import asynccontextmanager as acm

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from sembr.api.intents import router

    conn_holder: dict = {}

    @acm
    async def lifespan(app: FastAPI):
        conn = await aiosqlite.connect(":memory:")
        await conn.execute("PRAGMA foreign_keys=ON")
        await init_intent_tables(conn)
        await init_match_seen_tables(conn)
        install_for_test(conn)
        conn_holder["conn"] = conn
        yield
        await conn.close()

    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    embedder = MagicMock()
    embedder.is_loaded = True
    embedder.model_version = "bge-m3_v1"
    embedder.aembed = AsyncMock(return_value=[[0.1] * 1024])
    app.state.embedder = embedder
    app.state.qdrant = MagicMock()
    app.state.scheduler = MagicMock()
    from pathlib import Path as _Path  # noqa: PLC0415
    from unittest.mock import MagicMock as _MM  # noqa: PLC0415

    app.state.settings = _MM()
    project_prompts = _Path(__file__).parent.parent / "prompts"

    body = {
        "name": "rollback-test",
        "text": "rollback intent",
        "channels": [{"type": "email", "to": ["a@example.com"]}],
    }

    with (
        patch("sembr.summarizer.templates.PROMPTS_DIR", project_prompts),
        patch("sembr.api.intents.get_conn", side_effect=lambda: conn_holder["conn"]),
        patch("sembr.api.intents.upsert_intent_point", AsyncMock()),
        patch("sembr.api.intents.delete_intent_point", AsyncMock()),
        patch("sembr.api.intents.update_intent_payload", AsyncMock()),
        patch("sembr.api.intents.clear_intent", AsyncMock()),
        patch("sembr.api.intents.reregister_intent_job", MagicMock()),
        patch("sembr.api.intents.unregister_intent_job", MagicMock()),
        patch(
            "sembr.api.intents.register_intent_job",
            side_effect=RuntimeError("scheduler exploded"),
        ),
    ):
        with TestClient(app) as http:
            resp = http.post("/intents", json=body)
            remaining = http.get("/intents").json()

    assert resp.status_code == 500
    assert remaining == []  # SQLite row rolled back

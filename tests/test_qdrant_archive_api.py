# SPDX-License-Identifier: Apache-2.0
"""Endpoint behaviour for ``/api/archive/search`` and ``/api/archive/stats``.

Auth-gate coverage (401 per endpoint, real middleware) lives at the bottom —
each endpoint asserts its own 401 rather than borrowing a sibling's coverage,
because the middleware gates by path prefix and a future route move would
silently drop an endpoint out of a "representative" test's reach.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sembr.api.archive import router as archive_router
from sembr.db import sqlite as _sqlite_mod
from sembr.db.feeds import init_feed_tables


def _ready_embedder(**overrides) -> SimpleNamespace:
    base = dict(
        is_loaded=True,
        model_version="bge-m3_v1",
        max_input_chars=8000,
        aembed=AsyncMock(return_value=[[0.1] * 4]),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _query_response(points: list) -> SimpleNamespace:
    return SimpleNamespace(points=points)


def _scored_point(pid: str, score: float, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(id=pid, score=score, payload=payload)


def _payload(**overrides) -> dict:
    base = {
        "url": "https://www.reuters.com/world/x",
        "title": "Vodafone ups guidance",
        "body": "Vodafone raised its outlook for the year",
        "published_at": "2026-07-27T06:49:53+00:00",
        "published_at_ts": 1785134993,
        "feed_id": 1,
        "embedding_model_version": "bge-m3_v1",
        "ingested_at_ts": 1785000000,
        "body_len": 40,
        "lang": "en",
        "url_domain": "reuters.com",
        "matched_intents": [29],
        "archived_at_ts": 1790000000,
    }
    base.update(overrides)
    return base


def _make_app(qdrant_client=None, embedder=None) -> FastAPI:
    app = FastAPI()
    app.include_router(archive_router)
    handle = MagicMock()
    handle.client = qdrant_client if qdrant_client is not None else MagicMock()
    app.state.qdrant = handle
    app.state.embedder = embedder if embedder is not None else _ready_embedder()
    return app


def _search(client: TestClient, body: dict):
    return client.post("/api/archive/search", json=body)


# ---------------------------------------------------------------------------
# Semantic mode
# ---------------------------------------------------------------------------


def test_semantic_happy_path_maps_fields():
    qc = MagicMock()
    qc.query_points = AsyncMock(
        return_value=_query_response([_scored_point("id-1", 0.83, _payload())])
    )
    app = _make_app(qdrant_client=qc)

    async def _seed():
        conn = await aiosqlite.connect(":memory:")
        await init_feed_tables(conn)
        await conn.execute(
            "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
            "VALUES (1, 'Reuters', 'http://r', 30)"
        )
        await conn.commit()
        _sqlite_mod._conn = conn
        _sqlite_mod._WRITE_LOCK = asyncio.Lock()
        return conn

    conn = asyncio.run(_seed())
    try:
        resp = _search(TestClient(app), {"query": "fed hawkish signals"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "semantic"
        assert data["warnings"] == []
        assert data["next_cursor"] is None
        (hit,) = data["hits"]
        assert hit["id"] == "id-1"
        assert hit["score"] == 0.83
        assert hit["title"] == "Vodafone ups guidance"
        assert hit["body"]  # include_body defaults to True
        assert hit["published_at"] == "2026-07-27T06:49:53+00:00"
        assert hit["published_at_ts"] == 1785134993
        assert hit["feed_id"] == 1
        assert hit["feed_name"] == "Reuters"
        assert hit["url_domain"] == "reuters.com"
        assert hit["lang"] == "en"
        assert hit["matched_intents"] == [29]
        assert hit["embedding_model_version"] == "bge-m3_v1"
    finally:
        asyncio.run(conn.close())
        _sqlite_mod._conn = None
        _sqlite_mod._WRITE_LOCK = None


def test_semantic_filter_construction():
    qc = MagicMock()
    qc.query_points = AsyncMock(return_value=_query_response([]))
    app = _make_app(qdrant_client=qc)

    resp = _search(
        TestClient(app),
        {
            "query": "港口拥堵",
            "limit": 7,
            "min_score": 0.4,
            "exclude_ids": ["00000000-0000-0000-0000-0000000000aa"],
            "ingested_from_ts": 100,
            "ingested_to_ts": 200,
            "published_from_ts": 50,
            "published_to_ts": 150,
            "feed_ids": [1, 2],
            "exclude_feed_ids": [9],
            "title_contains": "霍尔木兹",
            "url_domains": ["reuters.com"],
            "min_body_len": 500,
            "matched_intent_ids": [29, 30],
            "langs": ["zh"],
        },
    )
    assert resp.status_code == 200

    kwargs = qc.query_points.call_args.kwargs
    assert kwargs["collection_name"] == "news_archive"
    assert kwargs["limit"] == 7
    assert kwargs["score_threshold"] == 0.4

    f = kwargs["query_filter"]
    by_key = {c.key: c for c in f.must}
    assert by_key["ingested_at_ts"].range.gte == 100
    assert by_key["ingested_at_ts"].range.lte == 200
    assert by_key["published_at_ts"].range.gte == 50
    assert by_key["published_at_ts"].range.lte == 150
    assert by_key["feed_id"].match.any == [1, 2]
    assert by_key["title"].match.text == "霍尔木兹"
    assert by_key["url_domain"].match.any == ["reuters.com"]
    assert by_key["body_len"].range.gte == 500
    assert by_key["matched_intents"].match.any == [29, 30]
    assert by_key["lang"].match.any == ["zh"]

    feed_not = [c for c in f.must_not if getattr(c, "key", None) == "feed_id"]
    assert feed_not and feed_not[0].match.any == [9]
    id_not = [c for c in f.must_not if getattr(c, "has_id", None)]
    assert id_not and id_not[0].has_id == ["00000000-0000-0000-0000-0000000000aa"]


def test_semantic_model_version_mismatch_warns():
    qc = MagicMock()
    qc.query_points = AsyncMock(
        return_value=_query_response(
            [_scored_point("id-1", 0.9, _payload(embedding_model_version="bge-m3_v0"))]
        )
    )
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"query": "anything"})
    assert resp.status_code == 200
    warnings = resp.json()["warnings"]
    assert any("generation mismatch" in w for w in warnings)


def test_semantic_embedder_not_ready_503():
    app = _make_app(embedder=_ready_embedder(is_loaded=False))
    resp = _search(TestClient(app), {"query": "x"})
    assert resp.status_code == 503
    assert "embedder" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def test_min_score_without_query_422():
    resp = _search(TestClient(_make_app()), {"min_score": 0.5})
    assert resp.status_code == 422


def test_cursor_with_query_422():
    resp = _search(
        TestClient(_make_app()),
        {"query": "x", "cursor": {"before_ts": 100, "boundary_ids": []}},
    )
    assert resp.status_code == 422


def test_param_caps_422():
    client = TestClient(_make_app())
    assert _search(client, {"limit": 101}).status_code == 422
    assert _search(client, {"query": "x" * 2001}).status_code == 422
    assert _search(client, {"exclude_ids": ["i"] * 1001}).status_code == 422


def test_unknown_fields_rejected_422():
    """Filter params come in singular/plural pairs — a typo silently ignored
    would return the whole archive as if filtered. extra=forbid must 422."""
    client = TestClient(_make_app())
    resp = _search(client, {"limit": 5, "matched_intent_id": 29, "feed_id": 3})
    assert resp.status_code == 422
    body = resp.text
    assert "matched_intent_id" in body

    # The cursor is round-tripped verbatim; a mistyped cursor field must
    # error too, not silently produce a cursor that excludes nothing.
    resp = _search(client, {"cursor": {"before_ts": 1, "boundary_id": ["x"]}})
    assert resp.status_code == 422


def test_blank_query_is_filter_mode_and_never_embedded():
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=([], None))
    embedder = _ready_embedder()
    app = _make_app(qdrant_client=qc, embedder=embedder)
    client = TestClient(app)

    for blank in ("", "   "):
        resp = _search(client, {"query": blank})
        assert resp.status_code == 200
        assert resp.json()["mode"] == "filter"
    embedder.aembed.assert_not_called()


def test_empty_include_filters_rejected_422():
    """[] on an include-filter would be truthiness-dropped and return the
    whole archive; blank title_contains likewise. Both must 422. exclude_*
    lists stay exempt (empty exclusion == no exclusion, unambiguous)."""
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=([], None))
    client = TestClient(_make_app(qdrant_client=qc))

    assert _search(client, {"feed_ids": []}).status_code == 422
    assert _search(client, {"langs": [], "query": "x"}).status_code == 422
    assert _search(client, {"matched_intent_ids": []}).status_code == 422
    assert _search(client, {"url_domains": []}).status_code == 422
    assert _search(client, {"title_contains": "   "}).status_code == 422
    # Exempt: empty exclusions are well-defined no-ops.
    assert _search(client, {"exclude_feed_ids": [], "exclude_ids": []}).status_code == 200


def _unexpected_response(code: int):
    from qdrant_client.http.exceptions import UnexpectedResponse

    return UnexpectedResponse(status_code=code, reason_phrase="x", content=b"detail", headers=None)


def test_qdrant_status_mapping_whitelist():
    """Real UnexpectedResponse mapping: only 400/422 are caller errors.
    404 (collection/alias missing) and 429 (rate limit) are service-side —
    agents treat HTTP 400 as 'fix your parameters, do not retry', which is
    the wrong instruction for both."""
    app = _make_app()
    client = TestClient(app)
    qc = app.state.qdrant.client

    for qdrant_code, expected_http in ((400, 400), (404, 503), (429, 503)):
        qc.scroll = AsyncMock(side_effect=_unexpected_response(qdrant_code))
        assert _search(client, {"limit": 5}).status_code == expected_http, qdrant_code

    qc.scroll = AsyncMock(side_effect=RuntimeError("connection refused"))
    assert _search(client, {"limit": 5}).status_code == 503


# ---------------------------------------------------------------------------
# Filter (listing) mode
# ---------------------------------------------------------------------------


def _listing_points(ts_list: list[int]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(id=f"id-{i}", payload=_payload(ingested_at_ts=ts))
        for i, ts in enumerate(ts_list)
    ]


def test_filter_mode_bare_listing_no_filter():
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=(_listing_points([100]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "filter"
    assert data["hits"][0]["score"] is None
    # fewer points than limit → no next page
    assert data["next_cursor"] is None

    kwargs = qc.scroll.call_args.kwargs
    assert kwargs["collection_name"] == "news_archive"
    assert kwargs["order_by"] == {"key": "ingested_at_ts", "direction": "desc"}
    assert "scroll_filter" not in kwargs
    assert kwargs["with_vectors"] is False


def test_filter_mode_full_page_returns_cursor():
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=(_listing_points([100, 90]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"limit": 2})
    assert resp.status_code == 200
    cursor = resp.json()["next_cursor"]
    assert cursor["before_ts"] == 90
    assert cursor["boundary_ids"] == ["id-1"]


def test_filter_mode_cursor_round_trip_and_same_ts_accumulation():
    qc = MagicMock()
    # Whole page at the boundary timestamp — the same-second cluster spans pages.
    qc.scroll = AsyncMock(return_value=(_listing_points([90, 90]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(
        TestClient(app),
        {"limit": 2, "cursor": {"before_ts": 90, "boundary_ids": ["prev-1", "prev-2"]}},
    )
    assert resp.status_code == 200

    kwargs = qc.scroll.call_args.kwargs
    assert kwargs["order_by"]["start_from"] == 90
    excluded = [c for c in kwargs["scroll_filter"].must_not if getattr(c, "has_id", None)]
    assert excluded and excluded[0].has_id == ["prev-1", "prev-2"]

    # Previous boundary ids carry over — dropping them would resurface those
    # points on the page after this one.
    cursor = resp.json()["next_cursor"]
    assert cursor["before_ts"] == 90
    assert cursor["boundary_ids"] == ["prev-1", "prev-2", "id-0", "id-1"]


def test_filter_mode_cursor_truncation_warns(monkeypatch):
    """Overflowing the boundary cap must be visible to the caller — the
    degradation is 'paging may not advance past this second', not a silent
    server-side log line."""
    import sembr.api.archive as archive_mod

    monkeypatch.setattr(archive_mod, "_CURSOR_BOUNDARY_CAP", 2)
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=(_listing_points([90, 90]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(
        TestClient(app),
        {"limit": 2, "cursor": {"before_ts": 90, "boundary_ids": ["p1", "p2"]}},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert any("pagination cannot exclude" in w for w in data["warnings"])
    # Truncated to the cap, keeping the most recent entries.
    assert len(data["next_cursor"]["boundary_ids"]) == 2


def test_feed_name_resolution_failure_warns():
    """No SQLite conn in this test → resolution fails → callers must be able
    to distinguish 'lookup failed' from 'feed deleted' via the warning."""
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=(_listing_points([100]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"limit": 5})
    assert resp.status_code == 200
    data = resp.json()
    assert data["hits"][0]["feed_name"] is None
    assert any("feed name resolution failed" in w for w in data["warnings"])


def test_filter_built_from_real_qdrant_models():
    """Guard against the test-stub regression: the Filter handed to Qdrant
    must be the real qdrant_client model (validated), not a duck-typed stub —
    the stub is fill-only now and this pins that property."""
    from qdrant_client.models import Filter as RealFilter

    qc = MagicMock()
    qc.query_points = AsyncMock(return_value=_query_response([]))
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"query": "x", "feed_ids": [1]})
    assert resp.status_code == 200
    assert isinstance(qc.query_points.call_args.kwargs["query_filter"], RealFilter)


def test_filter_mode_include_body_false():
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=(_listing_points([100]), None))
    app = _make_app(qdrant_client=qc)

    resp = _search(TestClient(app), {"limit": 5, "include_body": False})
    assert resp.status_code == 200
    hit = resp.json()["hits"][0]
    assert hit["body"] is None
    assert hit["body_len"] == 40  # metadata still present for UI list views


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def test_stats_counts_and_edges():
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=5))

    async def fake_scroll(**kwargs):
        direction = kwargs["order_by"]["direction"]
        ts = 10 if direction == "asc" else 99
        return [SimpleNamespace(id="x", payload={"ingested_at_ts": ts})], None

    qc.scroll = AsyncMock(side_effect=fake_scroll)
    app = _make_app(qdrant_client=qc)

    resp = TestClient(app).get("/api/archive/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points_count"] == 5
    assert data["earliest_ingested_at_ts"] == 10
    assert data["latest_ingested_at_ts"] == 99
    assert qc.count.call_args.kwargs["exact"] is True


def test_stats_empty_collection():
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=0))
    qc.scroll = AsyncMock(return_value=([], None))
    app = _make_app(qdrant_client=qc)

    resp = TestClient(app).get("/api/archive/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["points_count"] == 0
    assert data["earliest_ingested_at_ts"] is None
    assert data["latest_ingested_at_ts"] is None


def _stats_qc(alias_target: str | None):
    qc = MagicMock()
    qc.count = AsyncMock(return_value=SimpleNamespace(count=1))
    qc.scroll = AsyncMock(return_value=([], None))
    aliases = []
    if alias_target is not None:
        aliases = [SimpleNamespace(alias_name="news_archive", collection_name=alias_target)]
    qc.get_aliases = AsyncMock(return_value=SimpleNamespace(aliases=aliases))
    return qc


def test_stats_alias_ok_true_when_generation_matches():
    app = _make_app(qdrant_client=_stats_qc("news_archive_bge-m3_v1"))
    resp = TestClient(app).get("/api/archive/stats")
    assert resp.status_code == 200
    assert resp.json()["alias_ok"] is True


def test_stats_alias_ok_false_on_mismatch_or_missing():
    app = _make_app(qdrant_client=_stats_qc("news_archive_old-model"))
    assert TestClient(app).get("/api/archive/stats").json()["alias_ok"] is False

    app = _make_app(qdrant_client=_stats_qc(None))
    assert TestClient(app).get("/api/archive/stats").json()["alias_ok"] is False


def test_stats_alias_ok_none_when_check_fails():
    qc = _stats_qc("news_archive_bge-m3_v1")
    qc.get_aliases = AsyncMock(side_effect=RuntimeError("qdrant hiccup"))
    app = _make_app(qdrant_client=qc)
    assert TestClient(app).get("/api/archive/stats").json()["alias_ok"] is None


# ---------------------------------------------------------------------------
# Auth gate — one 401 assertion per endpoint, real middleware
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-test-token")
    from sembr.config import get_settings

    get_settings.cache_clear()
    app = _make_app()
    from sembr.dashboard.auth import DashboardTokenMiddleware

    app.add_middleware(DashboardTokenMiddleware)
    yield app
    get_settings.cache_clear()


def test_search_requires_token_401(auth_app):
    resp = TestClient(auth_app).post("/api/archive/search", json={"limit": 1})
    assert resp.status_code == 401


def test_stats_requires_token_401(auth_app):
    resp = TestClient(auth_app).get("/api/archive/stats")
    assert resp.status_code == 401


def test_search_with_token_passes_gate(auth_app):
    qc = MagicMock()
    qc.scroll = AsyncMock(return_value=([], None))
    auth_app.state.qdrant.client = qc
    resp = TestClient(auth_app).post(
        "/api/archive/search",
        json={"limit": 1},
        headers={"X-Dashboard-Token": "secret-test-token"},
    )
    assert resp.status_code == 200


def test_stats_with_token_passes_gate(auth_app):
    auth_app.state.qdrant.client = _stats_qc("news_archive_bge-m3_v1")
    resp = TestClient(auth_app).get(
        "/api/archive/stats",
        headers={"X-Dashboard-Token": "secret-test-token"},
    )
    assert resp.status_code == 200

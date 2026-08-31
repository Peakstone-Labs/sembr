# SPDX-License-Identifier: Apache-2.0
"""Endpoint behaviour for ``POST /api/news/search``.

The endpoint fans out to two Qdrant collections and merges the results; the
caller is never told that. Most of what these tests pin is therefore about the
seam: that a merged page equals the top-k of the union, that a point present in
both stores appears once, that a partial failure never comes back as a
short-but-successful answer, and that nothing in the response or the request
schema names a store.

Auth-gate coverage (401, real middleware) lives at the bottom; the sibling
``/api/dashboard/maintenance/qdrant_stats`` endpoint asserts its own 401 in
``test_api_maintenance.py`` rather than borrowing this one, because the
middleware gates by path prefix and the two endpoints live under different
prefixes.
"""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client.models import Filter as RealFilter

from sembr.api.news_search import router as news_search_router
from sembr.db import sqlite as _sqlite_mod
from sembr.db.feeds import init_feed_tables
from sembr.db.intents import init_intent_tables
from sembr.db.match_seen import init_match_seen_tables
from sembr.vector_store.news import ALIAS_NAME
from sembr.vector_store.news_archive import ARCHIVE_ALIAS

_CURRENT = ALIAS_NAME
_ARCHIVE = ARCHIVE_ALIAS


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sqlite_db():
    """One in-memory DB per test, with a feed and a couple of match_seen rows.

    Seeded for every test in this module so that a feed-name or matched-intent
    lookup failure shows up as the warning it is, instead of hiding inside an
    assertion about some unrelated field.
    """

    async def _seed(conn):
        await init_feed_tables(conn)
        await init_intent_tables(conn)
        await init_match_seen_tables(conn)
        await conn.execute(
            "INSERT INTO feeds (id, name, url, poll_interval_minutes) "
            "VALUES (1, 'Reuters', 'http://r', 30)"
        )
        await conn.execute(
            "INSERT INTO intents (id, name, text) VALUES (29, 'fed', 'fed'), (30, 'hz', 'hz')"
        )
        await conn.execute(
            "INSERT INTO match_seen (intent_id, article_id) VALUES "
            "(29, 'cur-1'), (30, 'cur-1'), (29, 'cur-2')"
        )
        await conn.commit()
        _sqlite_mod._conn = conn
        _sqlite_mod._WRITE_LOCK = asyncio.Lock()

    async def _open():
        conn = await aiosqlite.connect(":memory:")
        try:
            await _seed(conn)
        except BaseException:
            # A leaked aiosqlite connection keeps its worker thread alive and
            # hangs the whole pytest process at exit, turning a one-line schema
            # error into "the suite never finishes".
            await conn.close()
            raise
        return conn

    conn = asyncio.run(_open())
    try:
        yield conn
    finally:
        asyncio.run(conn.close())


def _ready_embedder(**overrides) -> SimpleNamespace:
    base = dict(
        is_loaded=True,
        model_version="bge-m3_v1",
        max_input_chars=8000,
        aembed=AsyncMock(return_value=[[0.1] * 4]),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


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
    }
    base.update(overrides)
    return base


def _scored(pid: str, score: float, **payload_overrides) -> SimpleNamespace:
    return SimpleNamespace(id=pid, score=score, payload=_payload(**payload_overrides))


def _listed(pid: str, ts: int, **payload_overrides) -> SimpleNamespace:
    return SimpleNamespace(
        id=pid, score=None, payload=_payload(ingested_at_ts=ts, **payload_overrides)
    )


class _FakeQdrant:
    """Per-collection dispatch, so a test can give the two segments different
    results (a single shared MagicMock cannot express "these came from
    different stores", which is the whole subject here)."""

    def __init__(self, semantic=None, listing=None):
        self._semantic = semantic if semantic is not None else {}
        self._listing = listing if listing is not None else {}
        self.query_calls: list[dict] = []
        self.scroll_calls: list[dict] = []

    @staticmethod
    def _resolve(table, name):
        value = table.get(name, [])
        if isinstance(value, Exception):
            raise value
        return value

    async def query_points(self, **kwargs):
        self.query_calls.append(kwargs)
        return SimpleNamespace(points=self._resolve(self._semantic, kwargs["collection_name"]))

    async def scroll(self, **kwargs):
        self.scroll_calls.append(kwargs)
        return self._resolve(self._listing, kwargs["collection_name"]), None

    def call_for(self, collection: str) -> dict:
        for call in self.query_calls + self.scroll_calls:
            if call["collection_name"] == collection:
                return call
        raise AssertionError(f"no call issued against {collection!r}")

    def called(self, collection: str) -> bool:
        return any(c["collection_name"] == collection for c in self.query_calls + self.scroll_calls)


def _make_app(qdrant_client=None, embedder=None, backfill_pending: object = 0) -> FastAPI:
    app = FastAPI()
    app.include_router(news_search_router)
    handle = MagicMock()
    handle.client = qdrant_client if qdrant_client is not None else _FakeQdrant()
    app.state.qdrant = handle
    app.state.embedder = embedder if embedder is not None else _ready_embedder()
    if backfill_pending is not ...:
        app.state.news_derived_backfill_pending = backfill_pending
    return app


def _search(app: FastAPI, body: dict):
    return TestClient(app).post("/api/news/search", json=body)


# ---------------------------------------------------------------------------
# Semantic mode — merge semantics
# ---------------------------------------------------------------------------


def test_semantic_happy_path_maps_fields():
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.83)], _ARCHIVE: []})
    resp = _search(_make_app(qc), {"query": "fed hawkish signals"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "semantic"
    assert data["warnings"] == []
    assert data["next_cursor"] is None
    (hit,) = data["hits"]
    assert hit["id"] == "cur-1"
    assert hit["score"] == 0.83
    assert hit["title"] == "Vodafone ups guidance"
    assert hit["body"]  # include_body defaults to True
    assert hit["published_at"] == "2026-07-27T06:49:53+00:00"
    assert hit["published_at_ts"] == 1785134993
    assert hit["feed_id"] == 1
    assert hit["feed_name"] == "Reuters"
    assert hit["url_domain"] == "reuters.com"
    assert hit["lang"] == "en"
    assert hit["embedding_model_version"] == "bge-m3_v1"


def test_semantic_queries_both_segments_concurrently():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    assert _search(_make_app(qc), {"query": "x"}).status_code == 200
    assert {c["collection_name"] for c in qc.query_calls} == {_CURRENT, _ARCHIVE}


def test_semantic_merges_two_segments_desc():
    qc = _FakeQdrant(
        semantic={
            _CURRENT: [_scored("c1", 0.9), _scored("c2", 0.5), _scored("c3", 0.4)],
            _ARCHIVE: [_scored("a1", 0.8), _scored("a2", 0.6), _scored("a3", 0.3)],
        }
    )
    resp = _search(_make_app(qc), {"query": "x", "limit": 4})

    assert resp.status_code == 200
    hits = resp.json()["hits"]
    assert [h["id"] for h in hits] == ["c1", "a1", "a2", "c2"]
    assert [h["score"] for h in hits] == [0.9, 0.8, 0.6, 0.5]


def test_semantic_merge_equals_union_topk():
    """R4: the merged page must equal top-k over the union of both stores.

    Positive control on the same data: taking only one segment does NOT equal
    the union's top-k, so the assertion is not passing for free.
    """
    rng = random.Random(20260831)
    for _ in range(30):
        limit = rng.randint(1, 8)
        cur = [(f"c{i}", round(rng.uniform(0, 1), 4)) for i in range(limit)]
        arc = [(f"a{i}", round(rng.uniform(0, 1), 4)) for i in range(limit)]
        qc = _FakeQdrant(
            semantic={
                _CURRENT: [_scored(p, s) for p, s in sorted(cur, key=lambda t: -t[1])],
                _ARCHIVE: [_scored(p, s) for p, s in sorted(arc, key=lambda t: -t[1])],
            }
        )
        resp = _search(_make_app(qc), {"query": "x", "limit": limit})
        got = [h["id"] for h in resp.json()["hits"]]

        expected = [p for p, _s in sorted(cur + arc, key=lambda t: -t[1])][:limit]
        assert got == expected

        single_segment = [p for p, _s in sorted(cur, key=lambda t: -t[1])][:limit]
        if {p for p, _ in arc} and expected != single_segment:
            assert got != single_segment


def test_merge_dedupes_by_id_current_wins():
    """D11: mid-migration a point exists in both stores. It must appear once,
    and the surviving copy must be the live one — otherwise the answer would
    depend on how far the retention job has progressed."""
    qc = _FakeQdrant(
        semantic={
            _CURRENT: [_scored("cur-1", 0.7, title="live copy")],
            _ARCHIVE: [_scored("cur-1", 0.7, title="archived copy", matched_intents=[99])],
        }
    )
    resp = _search(_make_app(qc), {"query": "x"})

    hits = resp.json()["hits"]
    assert len(hits) == 1
    assert hits[0]["title"] == "live copy"
    # …and its matched intents come from the live table, not the frozen payload
    assert hits[0]["matched_intents"] == [29, 30]


def test_response_has_no_shard_markers():
    """D2: neither the response nor the request schema may name a store."""
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.5, archived_at_ts=1790000000)]})
    resp = _search(_make_app(qc), {"query": "x"})

    (hit,) = resp.json()["hits"]
    assert "archived_at_ts" not in hit
    assert "source" not in hit
    assert "segment" not in hit

    rejected = _search(_make_app(_FakeQdrant()), {"query": "x", "scope": "archive"})
    assert rejected.status_code == 422


# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------


def test_semantic_filter_construction():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    resp = _search(
        _make_app(qc),
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
            "langs": ["zh"],
        },
    )
    assert resp.status_code == 200

    for collection in (_CURRENT, _ARCHIVE):
        kwargs = qc.call_for(collection)
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
        assert by_key["lang"].match.any == ["zh"]

        feed_not = [c for c in f.must_not if getattr(c, "key", None) == "feed_id"]
        assert feed_not and feed_not[0].match.any == [9]
        id_not = [c for c in f.must_not if getattr(c, "has_id", None)]
        assert id_not and id_not[0].has_id == ["00000000-0000-0000-0000-0000000000aa"]


def test_filter_passed_to_client_is_real_qdrant_model():
    """R7: catches a test-suite stub that replaced qdrant_client.models with
    duck types — every filter assertion in this file would then be vacuous."""
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    _search(_make_app(qc), {"query": "x", "feed_ids": [1]})
    assert isinstance(qc.call_for(_CURRENT)["query_filter"], RealFilter)


def test_intent_filter_current_uses_has_id_from_sqlite():
    """D7: the current store keeps no matched-intent payload, so the filter is
    the article-id set read live from match_seen."""
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    resp = _search(_make_app(qc), {"query": "x", "matched_intent_ids": [29]})
    assert resp.status_code == 200

    f = qc.call_for(_CURRENT)["query_filter"]
    has_id = [c for c in f.must if getattr(c, "has_id", None) is not None]
    assert has_id and has_id[0].has_id == ["cur-1", "cur-2"]
    assert not [c for c in f.must if getattr(c, "key", None) == "matched_intents"]


def test_intent_filter_archive_uses_payload_match_any():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    _search(_make_app(qc), {"query": "x", "matched_intent_ids": [29, 30]})

    f = qc.call_for(_ARCHIVE)["query_filter"]
    by_key = {getattr(c, "key", None): c for c in f.must}
    assert by_key["matched_intents"].match.any == [29, 30]
    assert not [c for c in f.must if getattr(c, "has_id", None) is not None]


def test_intent_filter_empty_set_skips_current_segment():
    """D8: `has_id: []` has no defined "restrict to nothing" meaning, so the
    segment is not queried at all."""
    qc = _FakeQdrant(semantic={_ARCHIVE: [_scored("a1", 0.5)]})
    resp = _search(_make_app(qc), {"query": "x", "matched_intent_ids": [4242]})

    assert resp.status_code == 200
    assert not qc.called(_CURRENT)
    assert qc.called(_ARCHIVE)
    assert [h["id"] for h in resp.json()["hits"]] == ["a1"]


def test_intent_id_set_over_cap_returns_400(monkeypatch):
    """D9: silently truncating the id set would under-return with no signal."""
    from sembr.api import news_search as mod

    async def _huge(_conn, _ids):
        return [f"id-{i}" for i in range(mod._INTENT_ID_FILTER_CAP + 1)]

    monkeypatch.setattr(mod, "article_ids_for_intents", _huge)
    resp = _search(_make_app(_FakeQdrant()), {"query": "x", "matched_intent_ids": [29]})

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "fewer intents" in detail
    # The detail must not send the caller narrowing a time window: the id set
    # comes from match_seen alone and would not shrink by one article.
    assert "independent of the time window" in detail


# ---------------------------------------------------------------------------
# matched_intents fill
# ---------------------------------------------------------------------------


def test_hit_matched_intents_current_from_sqlite():
    """L2: the old single-store code read a payload key the current store does
    not have, so every live hit reported "matched no intent"."""
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.9), _scored("cur-2", 0.8)]})
    hits = _search(_make_app(qc), {"query": "x"}).json()["hits"]

    by_id = {h["id"]: h["matched_intents"] for h in hits}
    assert by_id["cur-1"] == [29, 30]
    assert by_id["cur-2"] == [29]


def test_hit_matched_intents_archive_from_payload():
    qc = _FakeQdrant(semantic={_ARCHIVE: [_scored("a1", 0.9, matched_intents=[31])]})
    (hit,) = _search(_make_app(qc), {"query": "x"}).json()["hits"]
    assert hit["matched_intents"] == [31]


def test_matched_intents_lookup_failure_degrades_with_warning(monkeypatch):
    """D19(c): this fills a display field and cannot change which articles came
    back, so it degrades — but silently returning [] would read as "matched
    nothing", which is the very confusion L2 removed."""
    from sembr.api import news_search as mod

    async def _boom(_conn, _ids):
        raise RuntimeError("sqlite gone")

    monkeypatch.setattr(mod, "matched_intents_for_articles", _boom)
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.9)]})
    resp = _search(_make_app(qc), {"query": "x"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["hits"][0]["matched_intents"] == []
    assert any("matched-intent lookup failed" in w for w in data["warnings"])


# ---------------------------------------------------------------------------
# Fan-out failure semantics (D19)
# ---------------------------------------------------------------------------


def test_segment_failure_fails_whole_request():
    """D19(a): returning the surviving segment's hits with a 200 is silent
    under-recall — the exact failure the derived fields, the backfill job and
    the pending warning all exist to prevent."""
    qc = _FakeQdrant(
        semantic={_CURRENT: [_scored("cur-1", 0.9)], _ARCHIVE: RuntimeError("archive down")}
    )
    resp = _search(_make_app(qc), {"query": "x"})

    assert resp.status_code == 503
    assert "news query failed" in resp.json()["detail"]


def test_segment_failure_maps_caller_errors_to_400():
    exc = RuntimeError("bad request")
    exc.status_code = 400
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: exc})
    assert _search(_make_app(qc), {"query": "x"}).status_code == 400


def test_segment_failure_status_whitelist_is_not_a_range():
    for code in (404, 408, 409, 429, 500):
        exc = RuntimeError("service state")
        exc.status_code = code
        qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: exc})
        assert _search(_make_app(qc), {"query": "x"}).status_code == 503


def test_intent_prequery_failure_fails_whole_request(monkeypatch):
    """D19(b): degrading to "no intent condition" would answer a narrow
    question with the entire corpus."""
    from sembr.api import news_search as mod

    async def _boom(_conn, _ids):
        raise RuntimeError("sqlite gone")

    monkeypatch.setattr(mod, "article_ids_for_intents", _boom)
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    resp = _search(_make_app(qc), {"query": "x", "matched_intent_ids": [29]})

    assert resp.status_code == 503
    # No query may have gone out at all — an unfiltered one would have been
    # answered with everything.
    assert qc.query_calls == []


# ---------------------------------------------------------------------------
# Model generation warning
# ---------------------------------------------------------------------------


def test_model_version_mismatch_detected_per_segment():
    """L1/D13: the pre-merge code sampled one payload from the merged list. If
    that representative came from the matching store, the other store's
    incomparable scores were mixed in silently."""
    qc = _FakeQdrant(
        semantic={
            _CURRENT: [_scored("cur-1", 0.99)],  # matches the live embedder
            _ARCHIVE: [_scored("a1", 0.2, embedding_model_version="bge-m3_v0")],
        }
    )
    warnings = _search(_make_app(qc), {"query": "x"}).json()["warnings"]

    assert any("generation mismatch" in w for w in warnings)
    # The warning must not disclose which store is stale. The old wording was
    # literally "archive point has ...", which is exactly the leak.
    assert not any("archive" in w or "news_current" in w or "segment" in w for w in warnings)


def test_no_version_warning_when_all_segments_match():
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.9)], _ARCHIVE: [_scored("a1", 0.8)]})
    assert _search(_make_app(qc), {"query": "x"}).json()["warnings"] == []


# ---------------------------------------------------------------------------
# Backfill pending warning (D15)
# ---------------------------------------------------------------------------


def test_derived_filter_warns_while_backfill_pending():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    warnings = _search(
        _make_app(qc, backfill_pending=113_000), {"query": "x", "langs": ["zh"]}
    ).json()["warnings"]
    assert any("still being backfilled" in w for w in warnings)


def test_no_backfill_warning_once_converged():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    assert (
        _search(_make_app(qc, backfill_pending=0), {"query": "x", "langs": ["zh"]}).json()[
            "warnings"
        ]
        == []
    )


def test_no_backfill_warning_without_a_derived_filter():
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    assert (
        _search(_make_app(qc, backfill_pending=113_000), {"query": "x", "feed_ids": [1]}).json()[
            "warnings"
        ]
        == []
    )


def test_backfill_pending_unknown_is_treated_as_pending():
    """D15: the flag being absent (process start before the first job round) or
    None (a failed count) must warn. Assuming zero would reopen the silent gap
    for the entire startup window."""
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    for pending in (..., None):
        warnings = _search(
            _make_app(qc, backfill_pending=pending), {"query": "x", "min_body_len": 100}
        ).json()["warnings"]
        assert any("still being backfilled" in w for w in warnings), pending


# ---------------------------------------------------------------------------
# Request validation (unchanged surface — L4 / L13 / L14)
# ---------------------------------------------------------------------------


def test_min_score_without_query_422():
    assert _search(_make_app(), {"min_score": 0.5}).status_code == 422


def test_cursor_with_query_422():
    assert (
        _search(
            _make_app(), {"query": "x", "cursor": {"before_ts": 1, "boundary_ids": []}}
        ).status_code
        == 422
    )


def test_param_caps_422():
    assert _search(_make_app(), {"query": "x", "limit": 101}).status_code == 422
    assert _search(_make_app(), {"query": "x", "min_score": 1.5}).status_code == 422
    assert _search(_make_app(), {"query": "x" * 2001}).status_code == 422


def test_unknown_fields_rejected_422():
    assert _search(_make_app(), {"query": "x", "feed_id": 1}).status_code == 422


def test_empty_include_filters_rejected_422():
    """L4: the filter builder truthiness-drops [], which would return the whole
    corpus as if filtered. The validator is what makes that safe — if it is
    ever weakened, these turn into silent full scans."""
    for field in ("feed_ids", "url_domains", "matched_intent_ids", "langs"):
        resp = _search(_make_app(), {"query": "x", field: []})
        assert resp.status_code == 422, field
        assert field in str(resp.json()["detail"])
    assert _search(_make_app(), {"query": "x", "title_contains": "   "}).status_code == 422
    # exclude_* lists stay valid: empty exclusion is unambiguous.
    qc = _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []})
    assert _search(_make_app(qc), {"query": "x", "exclude_ids": []}).status_code == 200


def test_blank_query_is_filter_mode_and_never_embedded():
    embedder = _ready_embedder()
    qc = _FakeQdrant(listing={_CURRENT: [], _ARCHIVE: []})
    resp = _search(_make_app(qc, embedder=embedder), {"query": "   "})

    assert resp.status_code == 200
    assert resp.json()["mode"] == "filter"
    embedder.aembed.assert_not_awaited()


def test_semantic_embedder_not_ready_503():
    resp = _search(_make_app(embedder=_ready_embedder(is_loaded=False)), {"query": "x"})
    assert resp.status_code == 503
    assert "embedder" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Filter mode / cursor
# ---------------------------------------------------------------------------


def test_filter_mode_bare_listing_no_filter():
    qc = _FakeQdrant(listing={_CURRENT: [], _ARCHIVE: []})
    resp = _search(_make_app(qc), {"limit": 5})

    assert resp.status_code == 200
    assert resp.json()["mode"] == "filter"
    for collection in (_CURRENT, _ARCHIVE):
        call = qc.call_for(collection)
        assert "scroll_filter" not in call
        assert call["order_by"] == {"key": "ingested_at_ts", "direction": "desc"}
        assert call["limit"] == 5


def test_filter_mode_merges_segments_newest_first():
    qc = _FakeQdrant(
        listing={
            _CURRENT: [_listed("c1", 300), _listed("c2", 100)],
            _ARCHIVE: [_listed("a1", 200), _listed("a2", 50)],
        }
    )
    hits = _search(_make_app(qc), {"limit": 3}).json()["hits"]
    assert [h["id"] for h in hits] == ["c1", "a1", "c2"]


def test_filter_cursor_spans_segments():
    """O5-A: one cursor addresses both stores, because ingested_at_ts is a
    global ordering key. Page 1 is all-current here; page 2 must cross into the
    archive with nothing skipped and nothing repeated."""
    app = _make_app(
        _FakeQdrant(listing={_CURRENT: [_listed("c1", 300), _listed("c2", 200)], _ARCHIVE: []})
    )
    page1 = _search(app, {"limit": 2}).json()
    assert [h["id"] for h in page1["hits"]] == ["c1", "c2"]
    cursor = page1["next_cursor"]
    assert cursor == {"before_ts": 200, "boundary_ids": ["c2"]}

    qc2 = _FakeQdrant(listing={_CURRENT: [], _ARCHIVE: [_listed("a1", 200), _listed("a2", 100)]})
    page2 = _search(_make_app(qc2), {"limit": 2, "cursor": cursor}).json()
    assert [h["id"] for h in page2["hits"]] == ["a1", "a2"]

    # The same cursor was replayed against BOTH stores…
    for collection in (_CURRENT, _ARCHIVE):
        call = qc2.call_for(collection)
        assert call["order_by"]["start_from"] == 200
        excluded = [c for c in call["scroll_filter"].must_not if getattr(c, "has_id", None)]
        assert excluded and excluded[0].has_id == ["c2"]


def test_filter_cursor_schema_unchanged():
    """A per-segment cursor pair would leak the sharding straight through a
    field the caller round-trips verbatim."""
    qc = _FakeQdrant(listing={_CURRENT: [_listed("c1", 300)], _ARCHIVE: [_listed("a1", 200)]})
    cursor = _search(_make_app(qc), {"limit": 2}).json()["next_cursor"]
    assert set(cursor) == {"before_ts", "boundary_ids"}


def test_filter_mode_partial_page_has_no_cursor():
    qc = _FakeQdrant(listing={_CURRENT: [_listed("c1", 300)], _ARCHIVE: []})
    assert _search(_make_app(qc), {"limit": 5}).json()["next_cursor"] is None


def test_filter_cursor_boundary_from_merged_page_not_per_segment():
    """L10: both segments returning `limit` rows still yields ONE page of
    `limit`; the boundary has to come from that page's tail."""
    qc = _FakeQdrant(
        listing={
            _CURRENT: [_listed("c1", 500), _listed("c2", 400)],
            _ARCHIVE: [_listed("a1", 450), _listed("a2", 300)],
        }
    )
    data = _search(_make_app(qc), {"limit": 2}).json()
    assert [h["id"] for h in data["hits"]] == ["c1", "a1"]
    assert data["next_cursor"] == {"before_ts": 450, "boundary_ids": ["a1"]}


def test_filter_cursor_same_ts_accumulates_across_pages():
    qc = _FakeQdrant(listing={_CURRENT: [_listed("c2", 200)], _ARCHIVE: [_listed("a1", 200)]})
    data = _search(
        _make_app(qc), {"limit": 2, "cursor": {"before_ts": 200, "boundary_ids": ["c1"]}}
    ).json()
    assert data["next_cursor"]["before_ts"] == 200
    assert data["next_cursor"]["boundary_ids"] == ["c1", "c2", "a1"]


def test_filter_mode_cursor_truncation_warns(monkeypatch):
    """L9: truncation drops EXCLUSIONS, so paging may repeat or stall — the
    caller has to see it, not just the server log."""
    from sembr.api import news_search as mod

    monkeypatch.setattr(mod, "_CURSOR_BOUNDARY_CAP", 2)
    qc = _FakeQdrant(
        listing={
            _CURRENT: [_listed("c1", 200), _listed("c2", 200)],
            _ARCHIVE: [_listed("a1", 200)],
        }
    )
    data = _search(_make_app(qc), {"limit": 3}).json()
    assert len(data["next_cursor"]["boundary_ids"]) == 2
    assert any("may not advance" in w for w in data["warnings"])


def test_filter_mode_include_body_false():
    qc = _FakeQdrant(listing={_CURRENT: [_listed("c1", 300)], _ARCHIVE: []})
    (hit,) = _search(_make_app(qc), {"limit": 5, "include_body": False}).json()["hits"]
    assert hit["body"] is None
    assert hit["title"]


def test_listing_segment_failure_fails_whole_request():
    qc = _FakeQdrant(listing={_CURRENT: [_listed("c1", 300)], _ARCHIVE: RuntimeError("down")})
    resp = _search(_make_app(qc), {"limit": 5})
    assert resp.status_code == 503
    assert "news listing failed" in resp.json()["detail"]


def test_feed_name_resolution_failure_warns(monkeypatch):
    from sembr.api import news_search as mod

    async def _boom(_conn, _ids):
        raise RuntimeError("db gone")

    monkeypatch.setattr(mod, "get_feed_names", _boom)
    qc = _FakeQdrant(semantic={_CURRENT: [_scored("cur-1", 0.5)]})
    data = _search(_make_app(qc), {"query": "x"}).json()

    assert data["hits"][0]["feed_name"] is None
    assert any("feed name resolution failed" in w for w in data["warnings"])


# ---------------------------------------------------------------------------
# Retired routes
# ---------------------------------------------------------------------------


def test_old_archive_routes_return_404():
    """Consumer Audit: the rename keeps no alias, so the old paths must be gone
    from the real app — not merely absent from a hand-built test app."""
    from sembr.main import app as real_app

    paths = {getattr(r, "path", None) for r in real_app.routes}
    assert "/api/news/search" in paths
    assert not any(p and p.startswith("/api/archive") for p in paths)


# ---------------------------------------------------------------------------
# Auth gate — real middleware
# ---------------------------------------------------------------------------


@pytest.fixture
def auth_app(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "secret-test-token")
    from sembr.config import get_settings

    get_settings.cache_clear()
    app = _make_app(
        _FakeQdrant(semantic={_CURRENT: [], _ARCHIVE: []}, listing={_CURRENT: [], _ARCHIVE: []})
    )
    from sembr.dashboard.auth import DashboardTokenMiddleware

    app.add_middleware(DashboardTokenMiddleware)
    yield app
    get_settings.cache_clear()


def test_news_search_401_without_token(auth_app):
    assert TestClient(auth_app).post("/api/news/search", json={"limit": 1}).status_code == 401


def test_news_search_with_token_passes_gate(auth_app):
    resp = TestClient(auth_app).post(
        "/api/news/search",
        json={"limit": 1},
        headers={"X-Dashboard-Token": "secret-test-token"},
    )
    assert resp.status_code == 200

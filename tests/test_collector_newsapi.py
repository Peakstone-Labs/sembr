"""Static unit tests for sembr.collector.newsapi.

Cover NewsApiSource.fetch single-source path, NewsApiMaster.tick aggregation
path, normalize_source_uri parity with FeedCreate.url validator, and the
edge cases enumerated in design.md Test Strategy table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import aiosqlite
import httpx
import pytest
import respx

import sembr.collector.newsapi as newsapi_mod
from sembr.collector.newsapi import (
    NewsApiMaster,
    NewsApiSource,
    RECOMMENDED_SOURCES,
    _PerFeedSince,
    _build_request_body,
    _classify_quality,
    _date_window,
    _should_stop_paginating,
    _to_raw_article,
    _universal_since_for_pagination,
    normalize_source_uri,
)
from sembr.collector.rss import FetchError
from sembr.config import Settings


# ---------------------------------------------------------------------------
# normalize_source_uri — parity with FeedCreate.url newsapi branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Reuters.com", "reuters.com"),
        ("https://Reuters.com", "reuters.com"),
        ("HTTPS://www.Reuters.com/", "reuters.com"),
        ("http://www.bbc.co.uk", "bbc.co.uk"),
        ("  reuters.com  ", "reuters.com"),
        ("reuters.com/", "reuters.com"),
        ("WWW.NYTIMES.COM", "nytimes.com"),
        ("https://www.theguardian.com/", "theguardian.com"),
    ],
)
def test_normalize_source_uri(raw: str, expected: str) -> None:
    assert normalize_source_uri(raw) == expected


def test_recommended_sources_are_already_normalized() -> None:
    """Sanity: the datalist must be pre-normalized so frontend matching is literal."""
    for s in RECOMMENDED_SOURCES:
        assert normalize_source_uri(s["uri"]) == s["uri"]


# ---------------------------------------------------------------------------
# _classify_quality — content_quality length thresholds (§A4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "title_only"),
        (50, "title_only"),
        (100, "title_only"),
        (101, "stub"),
        (300, "stub"),  # paywalled bloomberg/wsj range
        (501, "summary"),
        (2000, "summary"),
        (2001, "full"),
        (50_000, "full"),
    ],
)
def test_classify_quality_thresholds(n: int, expected: str) -> None:
    assert _classify_quality(n) == expected


# ---------------------------------------------------------------------------
# _date_window — first-pull fallback
# ---------------------------------------------------------------------------


def test_date_window_first_pull_uses_now_minus_1d() -> None:
    """All-null sinces → dateStart = (now - 1d).date()."""
    start, end = _date_window([None, None, None])
    today = datetime.now(timezone.utc).date()
    assert end == today.isoformat()
    expected_start = (today - timedelta(days=1)).isoformat()
    # Allow boundary at midnight where start could equal today already
    assert start in (expected_start, today.isoformat())


def test_date_window_partial_null_falls_back_to_now_minus_1d() -> None:
    """Any null since → fall back to first-pull window so the unseen feed isn't blind."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=3)
    start, _ = _date_window([yesterday, None])
    today = datetime.now(timezone.utc).date()
    assert start == (today - timedelta(days=1)).isoformat()


def test_date_window_all_known_uses_min_since() -> None:
    s1 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    s2 = datetime(2026, 5, 3, 12, 0, tzinfo=timezone.utc)
    start, _ = _date_window([s1, s2])
    assert start == "2026-05-01"


# ---------------------------------------------------------------------------
# _to_raw_article — newsapi article dict → RawArticle field mapping
# ---------------------------------------------------------------------------


def test_to_raw_article_uses_url_not_uri() -> None:
    """⚠ url comes from article['url'] (real link), NEVER article['uri'] (internal id)."""
    raw = {
        "uri": "9209675915",
        "url": "https://www.reuters.com/world/story-1",
        "title": "Story 1",
        "body": "x" * 3000,
        "dateTime": "2026-05-08T14:32:11Z",
        "source": {"uri": "reuters.com", "title": "Reuters"},
    }
    article = _to_raw_article(raw)
    assert article is not None
    assert article.url == "https://www.reuters.com/world/story-1"
    assert article.url != "9209675915"
    assert article.content_quality == "full"
    assert article.published_at is not None
    assert article.published_at.tzinfo is not None
    assert article.published_at.tzinfo.utcoffset(article.published_at) == timedelta(0)


def test_to_raw_article_classifies_lengths() -> None:
    cases = [
        ("x" * 3000, "full"),
        ("x" * 300, "stub"),  # paywalled-style
        ("x" * 50, "title_only"),
    ]
    for body, expected in cases:
        article = _to_raw_article(
            {
                "url": "https://example.com/a",
                "title": "T",
                "body": body,
                "dateTime": "2026-05-08T00:00:00Z",
                "source": {"uri": "example.com"},
            }
        )
        assert article is not None
        assert article.content_quality == expected


def test_to_raw_article_drops_missing_url_or_title() -> None:
    assert _to_raw_article({"title": "x", "body": "y"}) is None
    assert _to_raw_article({"url": "https://x", "body": "y"}) is None
    assert _to_raw_article({}) is None


# ---------------------------------------------------------------------------
# _build_request_body — fixed fields + date window + categoryUri
# ---------------------------------------------------------------------------


def test_build_request_body_has_fixed_fields_and_dates() -> None:
    settings = Settings(newsapi_categories="Business,Technology")
    body = _build_request_body(
        api_key="KEY",
        source_uris=["reuters.com", "bbc.com"],
        settings=settings,
        date_start="2026-05-07",
        date_end="2026-05-08",
    )
    assert body["apiKey"] == "KEY"
    assert body["sourceUri"] == ["reuters.com", "bbc.com"]
    assert body["categoryUri"] == ["news/Business", "news/Technology"]
    assert body["dateStart"] == "2026-05-07"
    assert body["dateEnd"] == "2026-05-08"
    # Fixed fields per §A2
    assert body["articlesCount"] == 100
    assert body["articlesPage"] == 1
    assert body["articlesSortBy"] == "date"
    assert body["resultType"] == "articles"
    assert body["dataType"] == "news"
    assert body["articleBodyLen"] == -1
    assert body["isDuplicateFilter"] == "keepAll"
    assert body["lang"] == "eng"
    assert body["timezone"] == "UTC"
    # forceMaxDataTimeWindow MUST NOT appear — we drive the window via dateStart/dateEnd
    assert "forceMaxDataTimeWindow" not in body


# ---------------------------------------------------------------------------
# NewsApiSource.fetch — single-source path used by feed fire
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_news_api_source_fetch_single_source(monkeypatch) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    # Force fresh Settings (don't leak module-level cache)
    from sembr.config import get_settings

    get_settings.cache_clear()

    payload = {
        "articles": {
            "results": [
                {
                    "url": "https://www.reuters.com/a",
                    "uri": "9999",
                    "title": "Article A",
                    "body": "x" * 3000,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "reuters.com"},
                },
            ],
            "totalResults": 1,
        }
    }
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, json=payload, headers={"req-tokens": "1.000"})
    )
    src = NewsApiSource("Reuters.com")
    articles = await src.fetch(since=None)
    assert len(articles) == 1
    assert articles[0].url == "https://www.reuters.com/a"
    assert articles[0].content_quality == "full"


@pytest.mark.asyncio
async def test_news_api_source_fetch_no_api_key_raises(monkeypatch) -> None:
    """🔴-1 fix: missing API key is a configuration failure → FetchError so
    collect_feed's FetchError branch fires (no cursor advance)."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "")
    from sembr.config import get_settings

    get_settings.cache_clear()
    src = NewsApiSource("reuters.com")
    with pytest.raises(FetchError, match="NEWSAPI_API_KEY"):
        await src.fetch(since=None)


@respx.mock
@pytest.mark.asyncio
async def test_news_api_source_fetch_http_error_raises(monkeypatch) -> None:
    """🔴-1 fix: HTTP 4xx/5xx must raise FetchError (mirrors RSSSource)."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    from sembr.config import get_settings

    get_settings.cache_clear()
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    src = NewsApiSource("reuters.com")
    with pytest.raises(FetchError, match="HTTP error"):
        await src.fetch(since=None)


@respx.mock
@pytest.mark.asyncio
async def test_news_api_source_fetch_json_parse_error_raises(monkeypatch) -> None:
    """🔴-1 fix: 200 + invalid JSON → FetchError (token already burned;
    log_token_usage runs before the parse so the spend is still recorded)."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "k")
    from sembr.config import get_settings

    get_settings.cache_clear()
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"req-tokens": "1.000"})
    )
    src = NewsApiSource("reuters.com")
    with pytest.raises(FetchError, match="JSON parse"):
        await src.fetch(since=None)


@pytest.mark.asyncio
async def test_news_api_source_health_reflects_key(monkeypatch) -> None:
    from sembr.config import get_settings

    monkeypatch.setenv("NEWSAPI_API_KEY", "")
    get_settings.cache_clear()
    src = NewsApiSource("reuters.com")
    assert await src.health() is False

    monkeypatch.setenv("NEWSAPI_API_KEY", "real-key")
    get_settings.cache_clear()
    assert await src.health() is True


def test_news_api_source_config_schema_is_empty() -> None:
    schema = NewsApiSource.config_schema()
    assert schema["type"] == "object"
    assert schema["properties"] == {}


# ---------------------------------------------------------------------------
# NewsApiMaster.tick — full aggregation path
# ---------------------------------------------------------------------------


async def _setup_inmem_db_with_feeds(rows: list[dict]) -> aiosqlite.Connection:
    """Build an in-memory SQLite stand-in for the master tick tests.

    `rows` are dicts with id / url / last_collected_at; we insert with
    source_type='newsapi' and enabled=1 unless overridden.
    """
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")
    from sembr.db.feeds import init_feed_tables
    from sembr.db.articles import init_article_tables
    from sembr.dashboard.events import init_event_log_tables

    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)
    for r in rows:
        await conn.execute(
            "INSERT INTO feeds (id, name, url, source_type, last_collected_at, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                r["id"],
                r.get("name", f"feed-{r['id']}"),
                r["url"],
                r.get("source_type", "newsapi"),
                r.get("last_collected_at"),
                int(r.get("enabled", 1)),
            ),
        )
    await conn.commit()
    return conn


@pytest.fixture
def patched_get_conn():
    """Register an in-memory connection as the singleton via install_for_test."""

    def _patch(conn):
        from sembr.db.sqlite import install_for_test

        install_for_test(conn)
        return conn

    return _patch


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_dispatch_to_feed_ids(monkeypatch, patched_get_conn) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()

    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com"},
            {"id": 2, "url": "bbc.com"},
            {"id": 3, "url": "wsj.com"},
        ]
    )
    patched_get_conn(conn)

    payload = {
        "articles": {
            "results": [
                {
                    "url": "https://r.com/a",
                    "title": "T1",
                    "body": "x" * 3000,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "reuters.com"},
                },
                {
                    "url": "https://b.com/a",
                    "title": "T2",
                    "body": "x" * 600,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "bbc.com"},
                },
                {
                    "url": "https://wsj.com/a",
                    "title": "T3",
                    "body": "x" * 250,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "wsj.com"},
                },
            ],
            "totalResults": 3,
        }
    }
    # v1.1 pagination: feeds have no last_collected_at → universal_since=None
    # → watermark cannot stop, so we rely on page 2 being empty (natural end)
    # to trigger dispatch. Matches real newsapi behavior when results < cap.
    empty = {"articles": {"results": [], "totalResults": 3}}
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=payload, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=empty, headers={"req-tokens": "1.000"}),
        ]
    )

    await NewsApiMaster().tick()

    async with conn.execute(
        "SELECT feed_id, COUNT(*) FROM pending_articles GROUP BY feed_id ORDER BY feed_id"
    ) as cur:
        rows = await cur.fetchall()
    assert rows == [(1, 1), (2, 1), (3, 1)]
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_zero_articles_advances_cursor(monkeypatch, patched_get_conn) -> None:
    """Even with empty results, every enabled feed gets last_collected_at advanced
    AND a fetch_log row with ok=True."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()

    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com"},
            {"id": 2, "url": "bbc.com"},
            {"id": 3, "url": "techcrunch.com"},
        ]
    )
    patched_get_conn(conn)

    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(
            200,
            json={"articles": {"results": [], "totalResults": 0}},
            headers={"req-tokens": "1.000"},
        )
    )

    await NewsApiMaster().tick()

    async with conn.execute(
        "SELECT id, last_collected_at FROM feeds WHERE source_type='newsapi' ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert all(r[1] is not None for r in rows), rows

    async with conn.execute("SELECT feed_id, ok FROM feed_fetch_log ORDER BY feed_id") as cur:
        log_rows = await cur.fetchall()
    assert {r[0] for r in log_rows} == {1, 2, 3}
    assert all(r[1] == 1 for r in log_rows)
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_token_header_logged(monkeypatch, patched_get_conn, caplog) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(
            200,
            json={"articles": {"results": [], "totalResults": 0}},
            headers={"req-tokens": "1.000"},
        )
    )
    with caplog.at_level(logging.INFO, logger="sembr.collector.newsapi"):
        await NewsApiMaster().tick()
    assert any("req-tokens=1.0" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_total_results_overflow_warns(
    monkeypatch, patched_get_conn, caplog
) -> None:
    """File name preserved (git-diff-friendly) but body rewritten for the
    watermark+cap pagination model.

    The legacy single-page implementation only logged a warning when
    totalResults exceeded articlesCount=100; the current pagination loop drives
    the same totalResults>articlesCount mock through the page loop and checks
    watermark stop kicks in on page 2 — i.e. only 2 HTTP calls, no legacy
    only-warn left.
    """
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(hours=2)).isoformat()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)
    page1 = {
        "articles": {
            "results": [
                {
                    "url": "https://r.com/p1",
                    "title": "Page1",
                    "body": "x" * 600,
                    "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": {"uri": "reuters.com"},
                },
            ],
            "totalResults": 150,
        }
    }
    # page 2 oldest is older than universal_since cut → watermark stop
    page2 = {
        "articles": {
            "results": [
                {
                    "url": "https://r.com/p2-old",
                    "title": "Page2Old",
                    "body": "x" * 600,
                    "dateTime": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": {"uri": "reuters.com"},
                },
            ],
            "totalResults": 150,
        }
    }
    page3 = {
        "articles": {
            "results": [
                {
                    "url": "https://r.com/p3",
                    "title": "ShouldNotFetch",
                    "body": "x" * 600,
                    "dateTime": (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": {"uri": "reuters.com"},
                },
            ],
            "totalResults": 150,
        }
    }
    route = respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=page2, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=page3, headers={"req-tokens": "1.000"}),
        ]
    )
    with caplog.at_level(logging.WARNING):
        await NewsApiMaster().tick()
    # Watermark stop on page 2 → exactly 2 HTTP calls, page 3 untouched.
    assert route.call_count == 2
    # The legacy only-warn message must be gone.
    assert not any("exceeds articlesCount=100" in r.getMessage() for r in caplog.records)
    # Watermark stop = unified dispatch ran → page 1 article landed.
    async with conn.execute("SELECT title FROM pending_articles ORDER BY title") as cur:
        rows = await cur.fetchall()
    assert "Page1" in {r[0] for r in rows}
    # ShouldNotFetch never attempted → never inserted.
    assert "ShouldNotFetch" not in {r[0] for r in rows}
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_dispatch_unknown_source_dropped(
    monkeypatch, patched_get_conn, caplog
) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    payload = {
        "articles": {
            "results": [
                {
                    "url": "https://vox.com/a",
                    "title": "Mystery",
                    "body": "x" * 200,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "vox.com"},
                },
            ],
            "totalResults": 1,
        }
    }
    # v1.1: no last_collected_at on the feed → page 2 must be empty so the
    # loop naturally ends and dispatch runs.
    empty = {"articles": {"results": [], "totalResults": 1}}
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=payload, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=empty, headers={"req-tokens": "1.000"}),
        ]
    )
    with caplog.at_level(logging.WARNING):
        await NewsApiMaster().tick()
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        n = (await cur.fetchone())[0]
    assert n == 0
    assert any("not in uri_map" in r.getMessage() for r in caplog.records)
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_since_client_side_cut(monkeypatch, patched_get_conn) -> None:
    """published_at <= feed.last_collected_at → drop the article."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(minutes=30)).isoformat()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(
            200,
            json={
                "articles": {
                    "results": [
                        {
                            "url": "https://r.com/now",
                            "title": "Fresh",
                            "body": "x" * 600,
                            "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "source": {"uri": "reuters.com"},
                        },
                        {
                            "url": "https://r.com/old1",
                            "title": "Older1",
                            "body": "x" * 600,
                            "dateTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "source": {"uri": "reuters.com"},
                        },
                        {
                            "url": "https://r.com/old2",
                            "title": "Older2",
                            "body": "x" * 600,
                            "dateTime": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "source": {"uri": "reuters.com"},
                        },
                    ],
                    "totalResults": 3,
                }
            },
            headers={"req-tokens": "1.000"},
        )
    )
    await NewsApiMaster().tick()
    async with conn.execute("SELECT title FROM pending_articles") as cur:
        rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"Fresh"}
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_http_error_does_not_advance_cursor(
    monkeypatch, patched_get_conn
) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(500, text="upstream broke")
    )
    await NewsApiMaster().tick()
    async with conn.execute("SELECT last_collected_at FROM feeds WHERE id=1") as cur:
        row = await cur.fetchone()
    assert row[0] is None  # no cursor advance on failure
    # Loop 6 🟡-1 v1.1: HTTP failure now also emits an ok=False fetch_log
    # row per feed (was 0 in v1.0, see review.md Loop 5 🟡-1). Cursor still
    # NOT advanced — atomicity preserved; only the failed *attempt* is
    # recorded so the dashboard sparkline can detect a stuck cohort.
    async with conn.execute(
        "SELECT feed_id, ok, items_seen, error_class FROM feed_fetch_log"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 1
    assert rows[0][1] == 0  # ok=False
    assert rows[0][2] == 0  # items_seen=0
    assert rows[0][3] == "http_error"
    await conn.close()


@pytest.mark.asyncio
async def test_master_tick_no_enabled_feeds_skips_silently(monkeypatch, patched_get_conn) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([])
    patched_get_conn(conn)
    # No httpx mock — if tick tried to call out it would explode
    await NewsApiMaster().tick()
    await conn.close()


# ===========================================================================
# Master-tick pagination (max-pages cap + watermark stop + integral dispatch)
# ===========================================================================


# ---------------------------------------------------------------------------
# _should_stop_paginating — pure function (sort-order-robust watermark stop)
# ---------------------------------------------------------------------------


def _mk_article(dt: datetime) -> dict:
    return {
        "url": f"https://r.com/{dt.timestamp()}",
        "title": "T",
        "body": "x" * 200,
        "dateTime": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"uri": "reuters.com"},
    }


def test_should_stop_paginating_orientation_robust() -> None:
    """Stop decision is invariant under page-internal sort
    direction. Same articles in desc and asc must produce the same stop bool.
    Failure here means the upstream silently flipped articleSortBy and the
    'last article' heuristic would have given wrong answers — i.e. the
    drift-guard for Q1."""
    now = datetime.now(timezone.utc)
    cut = now - timedelta(hours=2)
    # 3 articles: now, now-1h, now-3h. Oldest (now-3h) <= cut → stop.
    arts = [
        _mk_article(now),
        _mk_article(now - timedelta(hours=1)),
        _mk_article(now - timedelta(hours=3)),
    ]
    assert _should_stop_paginating(arts, cut) is True
    assert _should_stop_paginating(list(reversed(arts)), cut) is True

    # All articles newer than cut → don't stop, regardless of order.
    fresh = [_mk_article(now), _mk_article(now - timedelta(minutes=30))]
    assert _should_stop_paginating(fresh, cut) is False
    assert _should_stop_paginating(list(reversed(fresh)), cut) is False


def test_should_stop_paginating_since_none_never_stops() -> None:
    """First-pull bootstrap: universal_since=None → defensive cap is the
    only stop mechanism, watermark never fires."""
    now = datetime.now(timezone.utc)
    arts = [_mk_article(now - timedelta(days=30))]
    assert _should_stop_paginating(arts, None) is False


def test_should_stop_paginating_empty_page_returns_false() -> None:
    """Empty page is handled separately by the caller (`if not page_results`
    break); helper just must not crash and must return False."""
    cut = datetime.now(timezone.utc)
    assert _should_stop_paginating([], cut) is False
    assert _should_stop_paginating([], None) is False


def test_should_stop_paginating_handles_missing_or_bad_datetime() -> None:
    """No parseable dateTime in any article → no signal to stop on → False
    (caller will rely on cap or empty-page break)."""
    cut = datetime.now(timezone.utc)
    bad = [{"title": "no dt"}, {"dateTime": ""}, {"dateTime": "garbage"}]
    assert _should_stop_paginating(bad, cut) is False


def test_universal_since_min_when_all_set() -> None:
    now = datetime.now(timezone.utc)
    pf = {
        "a": _PerFeedSince(feed_id=1, since=now - timedelta(hours=2)),
        "b": _PerFeedSince(feed_id=2, since=now - timedelta(hours=5)),
    }
    out = _universal_since_for_pagination(pf)
    assert out is not None
    assert out == now - timedelta(hours=5)


def test_universal_since_none_when_any_feed_first_pull() -> None:
    now = datetime.now(timezone.utc)
    pf = {
        "a": _PerFeedSince(feed_id=1, since=now - timedelta(hours=2)),
        "b": _PerFeedSince(feed_id=2, since=None),
    }
    assert _universal_since_for_pagination(pf) is None


# ---------------------------------------------------------------------------
# Master tick pagination — SC1 / SC2 / SC4
# ---------------------------------------------------------------------------


def _page_envelope(results: list[dict], total: int) -> dict:
    return {"articles": {"results": results, "totalResults": total}}


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_pagination_watermark_stops(monkeypatch, patched_get_conn) -> None:
    """SC1 v1.1: page1 newest, page2 mid, page3 oldest (≤ since) → only 2
    HTTP calls; page3 NOT requested; pending_articles contains only the
    ≥ since portion of pages 1+2."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(hours=4)).isoformat()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)

    # page 1: newest (now) + (now-1h) — both > cut
    page1 = _page_envelope(
        [
            {
                "url": "https://r.com/p1a",
                "title": "P1A",
                "body": "x" * 300,
                "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
            {
                "url": "https://r.com/p1b",
                "title": "P1B",
                "body": "x" * 300,
                "dateTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
        ],
        total=250,
    )
    # page 2: (now-3h, now-5h) — oldest ≤ cut(now-4h) → watermark stop
    # The article at now-5h is still loaded into all_results, but the
    # per-article since-cut drops it during dispatch.
    page2 = _page_envelope(
        [
            {
                "url": "https://r.com/p2a",
                "title": "P2A",
                "body": "x" * 300,
                "dateTime": (now - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
            {
                "url": "https://r.com/p2b-old",
                "title": "P2BOld",
                "body": "x" * 300,
                "dateTime": (now - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
        ],
        total=250,
    )
    # page 3: must NEVER be hit; if respx side_effect runs out it raises.
    page3 = _page_envelope(
        [
            {
                "url": "https://r.com/p3",
                "title": "P3SHOULDNOTFETCH",
                "body": "x" * 300,
                "dateTime": (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
        ],
        total=250,
    )
    route = respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=page2, headers={"req-tokens": "1.000"}),
            httpx.Response(200, json=page3, headers={"req-tokens": "1.000"}),
        ]
    )

    await NewsApiMaster().tick()

    # Exactly 2 HTTP calls (watermark stop on page 2).
    assert route.call_count == 2

    async with conn.execute("SELECT title FROM pending_articles ORDER BY title") as cur:
        rows = await cur.fetchall()
    titles = {r[0] for r in rows}
    # P1A, P1B, P2A landed (all > cut). P2BOld dropped by the since-cut.
    # P3SHOULDNOTFETCH never even fetched.
    assert "P1A" in titles
    assert "P1B" in titles
    assert "P2A" in titles
    assert "P2BOld" not in titles
    assert "P3SHOULDNOTFETCH" not in titles

    async with conn.execute("SELECT feed_id, ok, items_seen FROM feed_fetch_log") as cur:
        log_rows = await cur.fetchall()
    assert len(log_rows) == 1
    assert log_rows[0][0] == 1
    assert log_rows[0][1] == 1
    # items_seen counts pages 1+2 = 4 (cross-page accumulation).
    assert log_rows[0][2] == 4

    async with conn.execute("SELECT last_collected_at FROM feeds WHERE id=1") as cur:
        row = await cur.fetchone()
    assert row[0] is not None  # cursor advanced (watermark stop = success)
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_pagination_cap_dropped(monkeypatch, patched_get_conn, caplog) -> None:
    """Every fetched page's articles are newer than
    universal_since (watermark never fires) → cap=10 reached → integral
    drop: pending_articles=0, last_collected_at unchanged, no fetch_event.
    """
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(days=30)).isoformat()  # very far back
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)

    # Each of the 10 pages returns articles all dated within last hour
    # (well above cut). Watermark stop never fires; cap fires after page 10.
    def _fresh_page(idx: int) -> dict:
        return _page_envelope(
            [
                {
                    "url": f"https://r.com/p{idx}-{i}",
                    "title": f"P{idx}-{i}",
                    "body": "x" * 200,
                    "dateTime": (now - timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "source": {"uri": "reuters.com"},
                }
                for i in range(2)
            ],
            total=2000,
        )

    side_effects = [
        httpx.Response(200, json=_fresh_page(p), headers={"req-tokens": "1.000"})
        for p in range(1, 11)
    ]
    route = respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=side_effects
    )

    with caplog.at_level(logging.WARNING):
        await NewsApiMaster().tick()

    # All 10 pages fetched (cap=10 default).
    assert route.call_count == 10
    # Cap warning issued.
    assert any("max_pages=10 cap reached" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]
    # Cap reached without watermark → no dispatch → 0 articles, no cursor advance.
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        n = (await cur.fetchone())[0]
    assert n == 0
    async with conn.execute("SELECT last_collected_at FROM feeds WHERE id=1") as cur:
        row = await cur.fetchone()
    assert row[0] == cut  # unchanged
    # Loop 6 🟡-1 v1.1: cap-reached path now writes one ok=False fetch_log
    # row per feed (was 0 in v1.0 + Loop 5; review.md 🟡-1) so the
    # dashboard sparkline reflects the stuck state.
    async with conn.execute(
        "SELECT feed_id, ok, items_seen, items_new, error_class FROM feed_fetch_log"
    ) as cur:
        log_rows = await cur.fetchall()
    assert len(log_rows) == 1
    fid, ok, seen, new, err = log_rows[0]
    assert fid == 1
    assert ok == 0
    assert seen == 0
    assert new == 0
    assert err == "cap_reached"
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_pagination_mid_failure_atomic(monkeypatch, patched_get_conn) -> None:
    """Page 1 OK, page 2 HTTP 500 → integral rollback.
    pending_articles=0, last_collected_at unchanged, no fetch_event."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(days=30)).isoformat()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)

    page1 = _page_envelope(
        [
            {
                "url": "https://r.com/p1",
                "title": "P1WOULDLAND",
                "body": "x" * 300,
                "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
        ],
        total=300,
    )
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"req-tokens": "1.000"}),
            httpx.Response(500, text="upstream broke"),
        ]
    )

    await NewsApiMaster().tick()

    # B-1 atomic: page 1 results never dispatched.
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        assert (await cur.fetchone())[0] == 0
    async with conn.execute("SELECT last_collected_at FROM feeds WHERE id=1") as cur:
        row = await cur.fetchone()
    assert row[0] == cut  # unchanged
    # Loop 6 🟡-1 v1.1: mid-page HTTP failure now writes ok=False fetch_log
    # rows per feed (was 0 in Loop 5). Atomicity preserved (no cursor advance,
    # no pending inserts), only the failed *attempt* is recorded.
    async with conn.execute(
        "SELECT feed_id, ok, items_seen, error_class FROM feed_fetch_log"
    ) as cur:
        log_rows = await cur.fetchall()
    assert len(log_rows) == 1
    fid, ok, seen, err = log_rows[0]
    assert fid == 1
    assert ok == 0
    assert seen == 0
    assert err == "http_error"
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_steady_state_one_token(monkeypatch, patched_get_conn, caplog) -> None:
    """SC3 v1.1: totalResults=80 (under 100) — page 1 oldest is ≤ cut →
    watermark stop after page 1 → 1 HTTP call, equivalent to v1.0 cost.
    Guards against regression where pagination accidentally always walks
    multiple pages."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(hours=1)).isoformat()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com", "last_collected_at": cut},
        ]
    )
    patched_get_conn(conn)

    # Page 1 contains an article at now-2h (older than cut) so watermark
    # stop fires on page 1.
    page1 = _page_envelope(
        [
            {
                "url": "https://r.com/a",
                "title": "A",
                "body": "x" * 300,
                "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
            {
                "url": "https://r.com/b-old",
                "title": "BOld",
                "body": "x" * 300,
                "dateTime": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": {"uri": "reuters.com"},
            },
        ],
        total=80,
    )
    route = respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"req-tokens": "1.000"}),
        ]
    )
    with caplog.at_level(logging.INFO, logger="sembr.collector.newsapi"):
        await NewsApiMaster().tick()
    assert route.call_count == 1
    assert any("req-tokens=1.0" in r.getMessage() for r in caplog.records)
    await conn.close()


# ---------------------------------------------------------------------------
# Sanity: fire path (NewsApiSource.fetch) still sends articlesPage=1.
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_json_parse_failure_emits_log(monkeypatch, patched_get_conn) -> None:
    """Loop 6 🟡-1: JSON parse failure writes ok=False fetch_log per feed
    (atomicity preserved — no cursor advance, no pending inserts)."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds(
        [
            {"id": 1, "url": "reuters.com"},
            {"id": 2, "url": "bbc.com"},
        ]
    )
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, content=b"not json", headers={"req-tokens": "1.000"})
    )
    await NewsApiMaster().tick()

    async with conn.execute(
        "SELECT feed_id, ok, error_class FROM feed_fetch_log ORDER BY feed_id"
    ) as cur:
        rows = await cur.fetchall()
    assert {(r[0], r[1], r[2]) for r in rows} == {
        (1, 0, "json_error"),
        (2, 0, "json_error"),
    }
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        assert (await cur.fetchone())[0] == 0
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_bad_articles_block_emits_log(monkeypatch, patched_get_conn) -> None:
    """Loop 6 🟡-1: malformed response (no 'articles' block) writes
    ok=False fetch_log per feed; cursor unchanged."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings

    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(
            200,
            json={"error": "rate-limited"},  # no 'articles' key
            headers={"req-tokens": "1.000"},
        )
    )
    await NewsApiMaster().tick()

    async with conn.execute("SELECT feed_id, ok, error_class FROM feed_fetch_log") as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0] == (1, 0, "bad_response")
    await conn.close()


def test_build_request_body_default_page_is_one() -> None:
    """page parameter defaults to 1 so single-page callers (fire path,
    legacy tests) keep v1.0 wire format identical."""
    settings = Settings(newsapi_categories="Business")
    body = _build_request_body(
        api_key="K",
        source_uris=["reuters.com"],
        settings=settings,
        date_start="2026-05-09",
        date_end="2026-05-10",
    )
    assert body["articlesPage"] == 1


def test_build_request_body_explicit_page() -> None:
    """master tick passes 1..max_pages."""
    settings = Settings(newsapi_categories="Business")
    body = _build_request_body(
        api_key="K",
        source_uris=["reuters.com"],
        settings=settings,
        date_start="2026-05-09",
        date_end="2026-05-10",
        page=7,
    )
    assert body["articlesPage"] == 7

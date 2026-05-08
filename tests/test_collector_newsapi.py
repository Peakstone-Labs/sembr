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
    _build_request_body,
    _classify_quality,
    _date_window,
    _to_raw_article,
    normalize_source_uri,
)
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
        (300, "stub"),       # paywalled bloomberg/wsj range
        (501, "summary"),
        (2000, "summary"),
        (2001, "full"),
        (50_000, "full"),
    ],
)
def test_classify_quality_thresholds(n: int, expected: str) -> None:
    assert _classify_quality(n) == expected


# ---------------------------------------------------------------------------
# _date_window — D8 first-pull fallback
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
# _to_raw_article — D19/§A3 field mapping
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
        ("x" * 300, "stub"),       # paywalled-style
        ("x" * 50, "title_only"),
    ]
    for body, expected in cases:
        article = _to_raw_article({
            "url": "https://example.com/a",
            "title": "T",
            "body": body,
            "dateTime": "2026-05-08T00:00:00Z",
            "source": {"uri": "example.com"},
        })
        assert article is not None
        assert article.content_quality == expected


def test_to_raw_article_drops_missing_url_or_title() -> None:
    assert _to_raw_article({"title": "x", "body": "y"}) is None
    assert _to_raw_article({"url": "https://x", "body": "y"}) is None
    assert _to_raw_article({}) is None


# ---------------------------------------------------------------------------
# _build_request_body — D18 fixed fields + D8 dates + categoryUri
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
    assert body["isDuplicateFilter"] == "skipDuplicates"
    assert body["lang"] == "eng"
    assert body["timezone"] == "UTC"
    # forceMaxDataTimeWindow MUST NOT appear (D8)
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


@respx.mock
@pytest.mark.asyncio
async def test_news_api_source_fetch_no_api_key_returns_empty(monkeypatch, caplog) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "")
    from sembr.config import get_settings
    get_settings.cache_clear()
    src = NewsApiSource("reuters.com")
    with caplog.at_level(logging.WARNING):
        result = await src.fetch(since=None)
    assert result == []
    assert any("NEWSAPI_API_KEY is empty" in r.message for r in caplog.records)


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

    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com"},
        {"id": 2, "url": "bbc.com"},
        {"id": 3, "url": "wsj.com"},
    ])
    patched_get_conn(conn)

    payload = {
        "articles": {
            "results": [
                {
                    "url": "https://r.com/a", "title": "T1",
                    "body": "x" * 3000,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "reuters.com"},
                },
                {
                    "url": "https://b.com/a", "title": "T2",
                    "body": "x" * 600,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "bbc.com"},
                },
                {
                    "url": "https://wsj.com/a", "title": "T3",
                    "body": "x" * 250,
                    "dateTime": "2026-05-08T10:00:00Z",
                    "source": {"uri": "wsj.com"},
                },
            ],
            "totalResults": 3,
        }
    }
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, json=payload, headers={"req-tokens": "1.000"})
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
    """D7: even with empty results, every enabled feed gets last_collected_at advanced
    AND a fetch_log row with ok=True."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings
    get_settings.cache_clear()

    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com"},
        {"id": 2, "url": "bbc.com"},
        {"id": 3, "url": "techcrunch.com"},
    ])
    patched_get_conn(conn)

    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, json={"articles": {"results": [], "totalResults": 0}},
                                    headers={"req-tokens": "1.000"})
    )

    await NewsApiMaster().tick()

    async with conn.execute(
        "SELECT id, last_collected_at FROM feeds WHERE source_type='newsapi' ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    assert all(r[1] is not None for r in rows), rows

    async with conn.execute(
        "SELECT feed_id, ok FROM feed_fetch_log ORDER BY feed_id"
    ) as cur:
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
        return_value=httpx.Response(200, json={"articles": {"results": [], "totalResults": 0}},
                                    headers={"req-tokens": "1.000"})
    )
    with caplog.at_level(logging.INFO, logger="sembr.collector.newsapi"):
        await NewsApiMaster().tick()
    assert any("req-tokens=1.0" in r.getMessage() for r in caplog.records), [r.getMessage() for r in caplog.records]
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_total_results_overflow_warns(monkeypatch, patched_get_conn, caplog) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(
            200,
            json={"articles": {"results": [], "totalResults": 150}},
            headers={"req-tokens": "1.000"},
        )
    )
    with caplog.at_level(logging.WARNING):
        await NewsApiMaster().tick()
    assert any("exceeds articlesCount=100" in r.getMessage() for r in caplog.records)
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_dispatch_unknown_source_dropped(monkeypatch, patched_get_conn, caplog) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, json={
            "articles": {
                "results": [
                    {
                        "url": "https://vox.com/a", "title": "Mystery",
                        "body": "x" * 200,
                        "dateTime": "2026-05-08T10:00:00Z",
                        "source": {"uri": "vox.com"},
                    },
                ],
                "totalResults": 1,
            }
        }, headers={"req-tokens": "1.000"})
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
    """D22: published_at <= feed.last_collected_at → drop the article."""
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings
    get_settings.cache_clear()
    now = datetime.now(timezone.utc)
    cut = (now - timedelta(minutes=30)).isoformat()
    conn = await _setup_inmem_db_with_feeds([
        {"id": 1, "url": "reuters.com", "last_collected_at": cut},
    ])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(200, json={
            "articles": {
                "results": [
                    {
                        "url": "https://r.com/now", "title": "Fresh",
                        "body": "x" * 600,
                        "dateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": {"uri": "reuters.com"},
                    },
                    {
                        "url": "https://r.com/old1", "title": "Older1",
                        "body": "x" * 600,
                        "dateTime": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": {"uri": "reuters.com"},
                    },
                    {
                        "url": "https://r.com/old2", "title": "Older2",
                        "body": "x" * 600,
                        "dateTime": (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "source": {"uri": "reuters.com"},
                    },
                ],
                "totalResults": 3,
            }
        }, headers={"req-tokens": "1.000"})
    )
    await NewsApiMaster().tick()
    async with conn.execute("SELECT title FROM pending_articles") as cur:
        rows = await cur.fetchall()
    assert {r[0] for r in rows} == {"Fresh"}
    await conn.close()


@respx.mock
@pytest.mark.asyncio
async def test_master_tick_http_error_does_not_advance_cursor(monkeypatch, patched_get_conn) -> None:
    monkeypatch.setenv("NEWSAPI_API_KEY", "test-key")
    from sembr.config import get_settings
    get_settings.cache_clear()
    conn = await _setup_inmem_db_with_feeds([{"id": 1, "url": "reuters.com"}])
    patched_get_conn(conn)
    respx.post("https://eventregistry.org/api/v1/article/getArticles").mock(
        return_value=httpx.Response(500, text="upstream broke")
    )
    await NewsApiMaster().tick()
    async with conn.execute(
        "SELECT last_collected_at FROM feeds WHERE id=1"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] is None  # D20: no cursor advance on failure
    async with conn.execute("SELECT COUNT(*) FROM feed_fetch_log") as cur:
        n = (await cur.fetchone())[0]
    assert n == 0  # D20: no fetch_event on failure
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

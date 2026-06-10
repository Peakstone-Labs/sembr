# SPDX-License-Identifier: Apache-2.0
"""QA-owned test cases for wisburg-api feature (Loop 2).

Covers the QA rows of the wisburg-api design Test Strategy table:
  - test_fetch_empty_summary_skipped    — explicit independent case
  - test_fetch_max_pages_cap_warns_and_delivers — 6-page full mock
  - test_fetch_429_raises_fetcherror    — list and detail 429
  - test_collect_feed_e2e_dedup         (SC#5)  — two collect_feed runs same window
  - test_fire_dry_run_wisburg           — _feed_dry_run NEW/DUP classification
  - test_health_reflects_key            — confirmed already covered by dev test
  - test_published_at_tz_conversion     — bad-format -> None branch
"""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import httpx
import pytest
import respx

from sembr.collector.rss import FetchError
from sembr.collector.wisburg import (
    WisburgSource,
    _md5_url_title,
    _parse_datetime_or_none,
)

REPORTS_URL = "https://api-omen.wisburg.com/api/reports"


# ---------------------------------------------------------------------------
# Test helper utilities (mirror dev test patterns)
# ---------------------------------------------------------------------------


def _use_key(monkeypatch, value: str = "test-key") -> None:
    from sembr.config import get_settings

    monkeypatch.setenv("WISBURG_API_KEY", value)
    get_settings.cache_clear()


def _envelope(data: dict) -> dict:
    return {"request_id": "req-1", "code": 200, "status": 0, "message": "success", "data": data}


def _list_page(items: list[dict], end_cursor: str | None = None) -> dict:
    return _envelope({"items": items, "page_info": {"end_cursor": end_cursor}})


def _detail(
    item_id: int,
    *,
    title: str = "标题",
    summary: str = "### 主要观点\n内容",
    dt: str = "2026-06-10T04:36:23+08:00",
) -> dict:
    return _envelope({"id": item_id, "title": title, "datetime": dt, "url": "", "summary": summary})


# ---------------------------------------------------------------------------
# empty summary skipped (independent QA case)
# Note: test_fetch_zero_delivered_warns (dev) covers the "whole batch skipped"
# path; this QA case pins the single-item skip path: one article has empty
# summary, the remaining valid article IS returned.
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_empty_summary_skipped(monkeypatch, caplog) -> None:
    """Detail with empty summary is skipped; other items in the same batch
    are returned normally. The skip emits a warning for the empty item."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": 10, "title": "good article", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": 11, "title": "empty body", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )
    # id=10 has valid summary
    respx.get(f"{REPORTS_URL}/10").mock(
        return_value=httpx.Response(
            200, json=_detail(10, title="good article", summary="# Summary\nContent")
        )
    )
    # id=11 has empty summary
    respx.get(f"{REPORTS_URL}/11").mock(
        return_value=httpx.Response(200, json=_detail(11, title="empty body", summary=""))
    )

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    assert len(articles) == 1
    assert articles[0].url == f"{REPORTS_URL}/10"
    assert articles[0].title == "good article"
    assert any("empty summary" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# max pages cap warns and delivers (6 full pages => 500 items + warning)
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_max_pages_cap_warns_and_delivers(monkeypatch, caplog) -> None:
    """When 6 full pages are available, _fetch_list stops at _MAX_PAGES=5
    (500 items), logs a warning about the cap, and returns the first 500."""
    _use_key(monkeypatch)

    page_count = [0]

    def list_router(request: httpx.Request) -> httpx.Response:
        page_count[0] += 1
        # Build a full page of 100 items, cursor = current page number as string
        current_page = page_count[0]
        items = [
            {
                "id": (current_page - 1) * 100 + i,
                "title": f"t{(current_page - 1) * 100 + i}",
                "datetime": "2026-06-10T04:00:00+08:00",
            }
            for i in range(1, 101)
        ]
        # Always return a full page with a cursor — 6 pages available
        return httpx.Response(200, json=_list_page(items, end_cursor=str(current_page)))

    respx.get(REPORTS_URL).mock(side_effect=list_router)

    # Mock all detail requests (ids 1..500 for first 5 pages)
    def detail_router(request: httpx.Request) -> httpx.Response:
        item_id = int(str(request.url).rsplit("/", 1)[1])
        return httpx.Response(
            200,
            json=_detail(item_id, title=f"t{item_id}"),
        )

    respx.get(url__regex=rf"{REPORTS_URL}/\d+$").mock(side_effect=detail_router)

    with caplog.at_level("WARNING", logger="sembr.collector.wisburg"):
        articles = await WisburgSource(REPORTS_URL).fetch(since=None)

    # Must deliver exactly 500 items (5 pages × 100)
    assert len(articles) == 500, f"Expected 500 items, got {len(articles)}"

    # Must emit a cap warning
    cap_warnings = [r for r in caplog.records if "cap" in r.message.lower()]
    assert cap_warnings, "Expected cap warning in logs"

    # The router was called page_count[0] times; must be exactly 5 (not 6)
    assert page_count[0] == 5, f"Expected 5 list pages fetched, got {page_count[0]}"


# ---------------------------------------------------------------------------
# 429 on list or detail raises FetchError
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fetch_429_list_raises_fetcherror(monkeypatch) -> None:
    """HTTP 429 on the list endpoint must raise FetchError (not return [])."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(429))

    with pytest.raises(FetchError):
        await WisburgSource(REPORTS_URL).fetch(since=None)


@respx.mock
@pytest.mark.asyncio
async def test_fetch_429_detail_raises_fetcherror(monkeypatch) -> None:
    """HTTP 429 on a detail request must raise FetchError (not skip the item)."""
    _use_key(monkeypatch)
    respx.get(REPORTS_URL).mock(
        return_value=httpx.Response(
            200,
            json=_list_page(
                [
                    {"id": 1, "title": "t1", "datetime": "2026-06-10T04:00:00+08:00"},
                    {"id": 2, "title": "t2", "datetime": "2026-06-10T04:00:00+08:00"},
                ]
            ),
        )
    )
    respx.get(f"{REPORTS_URL}/1").mock(return_value=httpx.Response(200, json=_detail(1)))
    respx.get(f"{REPORTS_URL}/2").mock(return_value=httpx.Response(429))

    with pytest.raises(FetchError):
        await WisburgSource(REPORTS_URL).fetch(since=None)


# ---------------------------------------------------------------------------
# SC#5 — collect_feed e2e dedup (aiosqlite in-memory DB)
# ---------------------------------------------------------------------------


async def _setup_wisburg_inmem_db() -> tuple[aiosqlite.Connection, int]:
    """Create an in-memory DB with feeds + article tables; return (conn, feed_id)."""
    conn = await aiosqlite.connect(":memory:")
    await conn.execute("PRAGMA foreign_keys=ON")

    from sembr.dashboard.events import init_event_log_tables
    from sembr.db.articles import init_article_tables
    from sembr.db.feeds import init_feed_tables

    await init_feed_tables(conn)
    await init_article_tables(conn)
    await init_event_log_tables(conn)

    await conn.execute(
        "INSERT INTO feeds (name, url, source_type, poll_interval_minutes) "
        "VALUES ('wisburg-reports', ?, 'wisburg-report', 60)",
        (REPORTS_URL,),
    )
    await conn.commit()
    async with conn.execute("SELECT id FROM feeds WHERE url=?", (REPORTS_URL,)) as cur:
        row = await cur.fetchone()
    feed_id: int = row[0]
    return conn, feed_id


@respx.mock
@pytest.mark.asyncio
async def test_collect_feed_e2e_dedup(monkeypatch) -> None:
    """SC#5: running collect_feed twice over the same window must insert articles
    only on the first run (items_new > 0). The second run re-fetches the same
    articles but dedup via MD5(url+title) in feed_items must yield items_new=0."""
    _use_key(monkeypatch)

    conn, feed_id = await _setup_wisburg_inmem_db()

    from sembr.db.sqlite import install_for_test

    install_for_test(conn)

    # Two articles that both runs will return
    list_body = _list_page(
        [
            {"id": 101, "title": "研报A", "datetime": "2026-06-10T04:00:00+08:00"},
            {"id": 102, "title": "研报B", "datetime": "2026-06-10T04:00:00+08:00"},
        ]
    )
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=list_body))
    respx.get(f"{REPORTS_URL}/101").mock(
        return_value=httpx.Response(200, json=_detail(101, title="研报A"))
    )
    respx.get(f"{REPORTS_URL}/102").mock(
        return_value=httpx.Response(200, json=_detail(102, title="研报B"))
    )

    from sembr.collector.scheduler import collect_feed

    # First run: both articles are new
    items_seen1, items_new1, _ = await collect_feed(
        feed_id, "wisburg-reports", REPORTS_URL, "wisburg-report", {}
    )
    assert items_seen1 == 2, f"Expected 2 items_seen on first run, got {items_seen1}"
    assert items_new1 == 2, f"Expected 2 items_new on first run, got {items_new1}"

    # Re-register same mocks so second call can replay them
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=list_body))
    respx.get(f"{REPORTS_URL}/101").mock(
        return_value=httpx.Response(200, json=_detail(101, title="研报A"))
    )
    respx.get(f"{REPORTS_URL}/102").mock(
        return_value=httpx.Response(200, json=_detail(102, title="研报B"))
    )

    # Second run within same overlap window: both articles are duplicates
    items_seen2, items_new2, results2 = await collect_feed(
        feed_id, "wisburg-reports", REPORTS_URL, "wisburg-report", {}
    )
    assert items_seen2 == 2, f"Expected 2 items_seen on second run, got {items_seen2}"
    assert items_new2 == 0, f"Expected items_new=0 on second run (dedup), got {items_new2}"
    # Each article in the second run must be classified DUP
    dup_statuses = [r["status"] for r in results2]
    assert all(s == "DUP" for s in dup_statuses), f"Expected all DUP, got {dup_statuses}"

    await conn.close()


# ---------------------------------------------------------------------------
# fire dry_run walks WisburgSource → NEW/DUP classification
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.asyncio
async def test_fire_dry_run_wisburg(monkeypatch) -> None:
    """_feed_dry_run with WisburgSource must correctly classify articles as
    NEW or DUP by checking fingerprint_exists without writing to the DB.

    Pattern: pre-seed one article fingerprint in feed_items, then fire dry_run
    with two articles (one matching, one new). Expect one NEW + one DUP.
    No writes to feed_items or pending_articles after dry_run.
    """
    _use_key(monkeypatch)

    conn, feed_id = await _setup_wisburg_inmem_db()

    from sembr.db.sqlite import install_for_test

    install_for_test(conn)

    # Pre-seed the fingerprint for article id=201 so it looks like a known DUP
    known_url = f"{REPORTS_URL}/201"
    known_title = "已知研报"
    known_md5 = _md5_url_title(known_url, known_title)
    await conn.execute(
        "INSERT OR IGNORE INTO feed_items (md5, feed_id) VALUES (?, ?)",
        (known_md5, feed_id),
    )
    await conn.commit()

    # Mock wisburg: 2 articles — id=201 (DUP) and id=202 (NEW)
    list_body = _list_page(
        [
            {"id": 201, "title": known_title, "datetime": "2026-06-10T04:00:00+08:00"},
            {"id": 202, "title": "新研报", "datetime": "2026-06-10T04:00:00+08:00"},
        ]
    )
    respx.get(REPORTS_URL).mock(return_value=httpx.Response(200, json=list_body))
    respx.get(f"{REPORTS_URL}/201").mock(
        return_value=httpx.Response(200, json=_detail(201, title=known_title))
    )
    respx.get(f"{REPORTS_URL}/202").mock(
        return_value=httpx.Response(200, json=_detail(202, title="新研报"))
    )

    from sembr.api.feeds_fire import _feed_dry_run
    from sembr.collector.fire_tasks import create_task

    task = create_task(feed_id=feed_id, dry_run=True)
    await _feed_dry_run(task, REPORTS_URL, "wisburg-report", {}, since=None)

    assert task.status == "done", f"Expected task status 'done', got {task.status!r}"
    assert task.articles_fetched == 2
    assert task.articles_new == 1

    statuses = {a["url"]: a["status"] for a in task.articles}
    assert statuses[known_url] == "DUP", (
        f"Expected DUP for known article, got {statuses[known_url]}"
    )
    assert statuses[f"{REPORTS_URL}/202"] == "NEW", "Expected NEW for fresh article"

    # Dry run must NOT have written any new rows to feed_items
    async with conn.execute("SELECT COUNT(*) FROM feed_items") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1, f"Dry run must not write to feed_items (expected 1 pre-seeded, got {count})"

    # Dry run must NOT have written any rows to pending_articles
    async with conn.execute("SELECT COUNT(*) FROM pending_articles") as cur:
        pcount = (await cur.fetchone())[0]
    assert pcount == 0, f"Dry run must not write to pending_articles (got {pcount})"

    await conn.close()


# ---------------------------------------------------------------------------
# health_reflects_key (QA verification: dev already has this; confirm)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reflects_key_qa_verification(monkeypatch) -> None:
    """QA verification: confirm health() returns False for empty key and
    True for non-empty key without making any remote requests."""
    from sembr.config import get_settings

    monkeypatch.setenv("WISBURG_API_KEY", "")
    get_settings.cache_clear()
    source = WisburgSource(REPORTS_URL)
    assert await source.health() is False, "health() must be False when key is empty"

    monkeypatch.setenv("WISBURG_API_KEY", "valid-key")
    get_settings.cache_clear()
    assert await source.health() is True, "health() must be True when key is non-empty"


# ---------------------------------------------------------------------------
# published_at_tz_conversion: bad format -> None (QA branch)
# The dev happy path tests the +08:00 -> UTC path; QA owns the error branch.
# ---------------------------------------------------------------------------


def test_published_at_bad_format_returns_none() -> None:
    """Malformed datetime strings must return None without raising."""
    bad_inputs = [
        "not-a-date",
        "2026/06/10 04:00:00",  # wrong separators — fromisoformat rejects slash-separated
        "Jun 10, 2026 4am",  # English locale string
        "",  # empty string
        None,  # non-string
        12345,  # integer
        "2026-13-01T00:00:00+08:00",  # month 13 — out-of-range month
    ]
    for bad in bad_inputs:
        result = _parse_datetime_or_none(bad)
        assert result is None, f"Expected None for bad input {bad!r}, got {result!r}"


def test_published_at_plus0800_to_utc() -> None:
    """+08:00 datetime is converted to UTC-aware (QA confirms happy path stays intact)."""
    result = _parse_datetime_or_none("2026-06-10T04:36:23+08:00")
    assert result is not None
    assert result.tzinfo == UTC
    # 04:36:23 CST (UTC+8) = 20:36:23 UTC (previous day)
    assert result == datetime(2026, 6, 9, 20, 36, 23, tzinfo=UTC)

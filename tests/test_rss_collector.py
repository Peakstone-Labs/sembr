"""Unit tests for the RSS collector (Windows-runnable, no Docker deps).

Uses respx to mock httpx and aiosqlite in-memory for DB tests.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from textwrap import dedent
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
import respx
import httpx

from sembr.collector.rss import RSSSource, _compute_md5
from sembr.db.feeds import (
    fingerprint_exists,
    init_feed_tables,
    insert_fingerprint,
    seed_initial_feeds,
)
from sembr.collector.initial_feeds import INITIAL_FEEDS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FULL_FEED = dedent("""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Test Feed</title>
        <item>
          <title>Article with full body</title>
          <link>https://example.com/article-1</link>
          <pubDate>Mon, 27 Apr 2026 10:00:00 +0000</pubDate>
          <content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">
            {"value": "This is a full article body that is longer than five hundred characters. " * 10}
          </content:encoded>
          <description>Short summary here.</description>
        </item>
      </channel>
    </rss>
""").strip().encode()

_STUB_FEED = dedent("""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Stub Feed</title>
        <item>
          <title>Only a title here</title>
          <link>https://example.com/stub-1</link>
          <pubDate>Mon, 27 Apr 2026 10:00:00 +0000</pubDate>
        </item>
      </channel>
    </rss>
""").strip().encode()

_MIXED_FEED = dedent("""
    <?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0">
      <channel>
        <title>Mixed Feed</title>
        <item>
          <title>New article</title>
          <link>https://example.com/new</link>
          <pubDate>Mon, 27 Apr 2026 12:00:00 +0000</pubDate>
          <description>New content.</description>
        </item>
        <item>
          <title>Old article</title>
          <link>https://example.com/old</link>
          <pubDate>Mon, 27 Apr 2026 08:00:00 +0000</pubDate>
          <description>Old content.</description>
        </item>
      </channel>
    </rss>
""").strip().encode()

_HTML_PAGE = b"<html><body>Not RSS</body></html>"


# ---------------------------------------------------------------------------
# Test: RSSSource.fetch — FULL content
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_rss_source_fetch_full():
    """Guardian-style source with full body → content_quality='full' only when content tag present."""
    feed_xml = dedent("""
        <?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel>
            <title>Full Feed</title>
            <item>
              <title>Deep investigation piece</title>
              <link>https://guardian.com/story-1</link>
              <pubDate>Mon, 27 Apr 2026 10:00:00 +0000</pubDate>
              <content:encoded>""" + ("Long article body. " * 40) + """</content:encoded>
              <description>Short intro.</description>
            </item>
          </channel>
        </rss>
    """).strip().encode()

    respx.get("https://example-full.com/rss").mock(
        return_value=httpx.Response(200, content=feed_xml)
    )
    src = RSSSource("https://example-full.com/rss")
    articles = await src.fetch()
    assert len(articles) == 1
    assert articles[0].content_quality == "full"
    assert len(articles[0].body) > 500


# ---------------------------------------------------------------------------
# Test: RSSSource.fetch — title_only (stub)
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_rss_source_fetch_stub():
    """Source with no body or summary → content_quality='title_only', body == title."""
    respx.get("https://example-stub.com/rss").mock(
        return_value=httpx.Response(200, content=_STUB_FEED)
    )
    src = RSSSource("https://example-stub.com/rss")
    articles = await src.fetch()
    assert len(articles) == 1
    assert articles[0].content_quality == "title_only"
    assert articles[0].body == articles[0].title


# ---------------------------------------------------------------------------
# Test: RSSSource.fetch — since filter
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_rss_source_fetch_since_filter():
    """Old article should be filtered out when since=T; new article passes through."""
    respx.get("https://example-mixed.com/rss").mock(
        return_value=httpx.Response(200, content=_MIXED_FEED)
    )
    since = datetime(2026, 4, 27, 10, 0, 0, tzinfo=timezone.utc)
    src = RSSSource("https://example-mixed.com/rss")
    articles = await src.fetch(since=since)
    assert len(articles) == 1
    assert articles[0].title == "New article"


# ---------------------------------------------------------------------------
# Test: RSSSource.fetch — bozo (non-RSS response)
# ---------------------------------------------------------------------------

@respx.mock
@pytest.mark.asyncio
async def test_rss_source_bozo():
    """feedparser bozo=True with no entries → empty list, no exception."""
    respx.get("https://example-bozo.com/rss").mock(
        return_value=httpx.Response(200, content=_HTML_PAGE)
    )
    src = RSSSource("https://example-bozo.com/rss")
    articles = await src.fetch()
    assert articles == []


# ---------------------------------------------------------------------------
# Test: seed_initial_feeds — idempotent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_idempotent():
    """Two consecutive seeds must produce exactly 23 rows, no IntegrityError."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_feed_tables(conn)
        first = await seed_initial_feeds(conn)
        second = await seed_initial_feeds(conn)
        async with conn.execute("SELECT COUNT(*) FROM feeds") as cur:
            row = await cur.fetchone()
        total = row[0]

    assert first == 23
    assert second == 0  # all ignored on second run
    assert total == 23


# ---------------------------------------------------------------------------
# Test: seed_initial_feeds — respects manual deletion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_seed_respects_deletion():
    """After deleting one feed, re-seed must NOT re-insert it (url UNIQUE + INSERT OR IGNORE)."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_feed_tables(conn)
        await seed_initial_feeds(conn)
        # Delete the first seeded feed by URL
        first_url = INITIAL_FEEDS[0]["url"]
        await conn.execute("DELETE FROM feeds WHERE url=?", (first_url,))
        await conn.commit()
        # Re-seed: deleted entry must not come back
        inserted = await seed_initial_feeds(conn)
        async with conn.execute("SELECT COUNT(*) FROM feeds WHERE url=?", (first_url,)) as cur:
            row = await cur.fetchone()

    assert inserted == 0  # or 0 since all other URLs already exist
    assert row[0] == 0  # deleted entry not re-inserted


# ---------------------------------------------------------------------------
# Test: collect_feed deduplication
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_collect_feed_dedup():
    """Same MD5 fingerprint must not be inserted twice into feed_items."""
    async with aiosqlite.connect(":memory:") as conn:
        await init_feed_tables(conn)
        await conn.execute(
            "INSERT INTO feeds (name, url, poll_interval_minutes) VALUES ('T', 'http://t.com', 30)"
        )
        await conn.commit()
        async with conn.execute("SELECT id FROM feeds LIMIT 1") as cur:
            feed_id = (await cur.fetchone())[0]

        md5 = "abc123deadbeef"
        assert not await fingerprint_exists(conn, md5)
        await insert_fingerprint(conn, md5, feed_id)
        assert await fingerprint_exists(conn, md5)
        # Second insert must be silently ignored
        await insert_fingerprint(conn, md5, feed_id)
        async with conn.execute("SELECT COUNT(*) FROM feed_items WHERE md5=?", (md5,)) as cur:
            count = (await cur.fetchone())[0]

    assert count == 1


# ---------------------------------------------------------------------------
# Test: MD5 fingerprint determinism
# ---------------------------------------------------------------------------

def test_md5_fingerprint():
    """_compute_md5(url, title) must be deterministic and match manual MD5."""
    url = "https://example.com/article"
    title = "Example Article Title"
    expected = hashlib.md5((url + title).encode()).hexdigest()
    assert _compute_md5(url, title) == expected
    assert _compute_md5(url, title) == _compute_md5(url, title)

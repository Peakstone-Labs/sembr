# SPDX-License-Identifier: Apache-2.0
"""newsapi feeds collapse onto a single 'newsapi' group_key in the dashboard.

Two reasons combined:
1. Visual grouping — they share a master job + one API endpoint.
2. Correctness — feed.url is a bare hostname like 'reuters.com' (no scheme),
   so the urlparse-based derive_group_key would otherwise return ''.
"""

from __future__ import annotations

import aiosqlite
import pytest

from sembr.dashboard.events import init_event_log_tables
from sembr.dashboard.read_model import list_feeds_with_meta
from sembr.db.feeds import init_feed_tables


@pytest.mark.asyncio
async def test_newsapi_feeds_collapse_to_newsapi_group_key() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await init_feed_tables(conn)
        await init_event_log_tables(conn)
        await conn.executemany(
            "INSERT INTO feeds (name, url, source_type, enabled) VALUES (?, ?, ?, 1)",
            [
                ("Reuters", "reuters.com", "newsapi"),
                ("BBC", "bbc.com", "newsapi"),
                ("Guardian RSS", "https://www.theguardian.com/world/rss", "rss"),
            ],
        )
        await conn.commit()

        page = await list_feeds_with_meta(
            conn,
            limit=10,
            offset=0,
            tag=None,
            q=None,
            proxy_hosts=frozenset(),
            scheduler=None,
        )

    by_name = {it.name: it for it in page.items}
    assert by_name["Reuters"].group_key == "newsapi"
    assert by_name["BBC"].group_key == "newsapi"
    # RSS path unchanged
    assert by_name["Guardian RSS"].group_key == "www.theguardian.com"
